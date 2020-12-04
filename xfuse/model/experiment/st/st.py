import itertools as it
import warnings
from copy import deepcopy
from functools import partial
from typing import Dict, List, NamedTuple, Optional

import numpy as np
import pyro as p
import torch
import torch.distributions.constraints as constraints
from pyro.contrib.autoname import scope
from pyro.distributions import (  # pylint: disable=no-name-in-module
    Delta,
    NegativeBinomial,
    Normal,
    OneHotCategorical,
    RelaxedOneHotCategoricalStraightThrough,
)
from torch.distributions import transform_to

from ....data import Data, Dataset
from ....data.slide import DataIterator, Slide
from ....data.utility.misc import estimate_spot_size, make_dataloader
from ....logging import Progressbar, DEBUG, INFO, log
from ....session import get, require
from ....utility.core import center_crop
from ....utility.state import (
    get_module,
    get_param,
    get_state_dict,
    load_state_dict,
)
from ....utility.tensor import checkpoint, isoftplus, sparseonehot, to_device
from ..image import Image


class MetageneDefault(NamedTuple):
    r"""Metagene initialization template"""

    scale: float
    profile: Optional[torch.Tensor]


def _encode_metagene_name(n: str):
    return f"!!metagene!{n}!!"


class ST(Image):
    r"""Spatial Transcriptomics experiment"""

    @property
    def tag(self):
        return "ST"

    def __init__(
        self,
        *args,
        metagenes: Optional[List[MetageneDefault]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        if metagenes is None:
            metagenes = [MetageneDefault(0.0, None)]

        if len(metagenes) == 0:
            raise ValueError("Needs at least one metagene")

        self.__metagenes: Dict[str, MetageneDefault] = {}
        self.__metagene_queue: List[str] = []
        for metagene in metagenes:
            self.add_metagene(metagene)

        self.__init_scale = None
        self.__init_rate = None
        self.__init_logits = None

        self.__allocated_genes: Optional[List[str]] = None
        self.__active_genes_id: Optional[int] = None
        self.__gene_indices: Optional[torch.Tensor] = None

    @property
    def metagenes(self) -> Dict[str, MetageneDefault]:
        r"""Metagene initialization templates"""
        return deepcopy(self.__metagenes)

    @property
    def _allocated_genes(self) -> List[str]:
        if self.__allocated_genes is None:
            self.__allocated_genes = require("genes")
        return self.__allocated_genes

    @property
    def _gene_indices(self) -> torch.Tensor:
        active_genes = require("genes")

        if (
            self.__gene_indices is None
            or id(active_genes) != self.__active_genes_id
        ):
            self.__active_genes_id = id(active_genes)

            log(DEBUG, "Computing new gene indices")

            nonexistant_mask = np.isin(
                active_genes, self._allocated_genes, invert=True
            )
            if nonexistant_mask.any():
                warnings.warn(
                    (
                        "Genes {} have not been allocated."
                        " Delayed allocation is currently not supported."
                    ).format(", ".join(active_genes[nonexistant_mask]))
                )
                active_genes = active_genes[nonexistant_mask]

            active_genes_idx = np.searchsorted(
                self._allocated_genes,
                active_genes,
                sorter=np.argsort(self._allocated_genes),
            )
            self.__gene_indices = torch.as_tensor(
                active_genes_idx, dtype=torch.long
            )

        return self.__gene_indices

    def add_metagene(self, metagene: Optional[MetageneDefault] = None):
        r"""
        Adds a new metagene, optionally initialized from a
        :class:`MetageneDefault`.
        """
        if metagene is None:
            metagene = MetageneDefault(0.0, None)

        if self.__metagene_queue != []:
            new_metagene = self.__metagene_queue.pop()
        else:
            new_metagene = f"{len(self.__metagenes) + 1:d}"
        assert new_metagene not in self.__metagenes

        log(INFO, "Adding metagene: %s", new_metagene)
        self.__metagenes.setdefault(new_metagene, metagene)

        return new_metagene

    def split_metagene(self, metagene: str):
        r"""Adds a new metagene by splitting an already existing metagene."""
        new_metagene = self.add_metagene(self.metagenes[metagene])

        log(INFO, "Copying metagene: %s -> %s", metagene, new_metagene)

        name = _encode_metagene_name(metagene)
        new_name = _encode_metagene_name(new_metagene)

        state_dict = get_state_dict()

        for pname in [
            pname for pname in state_dict.params.keys() if name in pname
        ]:
            new_pname = pname.replace(name, new_name)
            log(DEBUG, "Copying param: %s -> %s", pname, new_pname)
            state_dict.params[new_pname] = deepcopy(state_dict.params[pname])

        for mname in [
            mname for mname in state_dict.modules.keys() if name in mname
        ]:
            new_mname = mname.replace(name, new_name)
            log(DEBUG, "Copying module: %s -> %s", mname, new_mname)
            state_dict.modules[new_mname] = deepcopy(state_dict.modules[mname])

        load_state_dict(state_dict)

        return new_metagene

    def remove_metagene(self, n, remove_params=False):
        r"""Removes a metagene"""
        if len(self.metagenes) == 1:
            raise RuntimeError("Cannot remove last metagene")

        log(INFO, "Removing metagene: %s", n)

        try:
            self.__metagenes.pop(n)
        except KeyError as exc:
            raise ValueError(
                f"Attempted to remove metagene {n}, which doesn't exist!"
            ) from exc

        self.__metagene_queue.append(n)

        if remove_params:
            store = p.get_param_store()
            optim = get("optimizer")
            pname = _encode_metagene_name(n)
            for x in [p for p in store.keys() if pname in p]:
                param = store[x].unconstrained()
                del store[x]
                if optim is not None:
                    del optim.optim_objs[param]

    def __init_globals(self):
        dataloader = require("dataloader")
        device = get("default_device")

        dataloader = make_dataloader(
            Dataset(
                Data(
                    slides={
                        k: Slide(
                            data=v.data,
                            # pylint: disable=unnecessary-lambda
                            # ^ Necessary for type checking to pass
                            iterator=lambda x: DataIterator(x),
                        )
                        for k, v in dataloader.dataset.data.slides.items()
                        if v.data.type == "ST"
                    },
                    design=dataloader.dataset.data.design,
                )
            ),
            num_workers=0,
            batch_size=100,
        )

        r2rp = transform_to(constraints.positive)

        scale = torch.zeros(1, requires_grad=True, device=device)
        rate = torch.zeros(
            len(self._allocated_genes), requires_grad=True, device=device
        )
        logits = torch.zeros(
            len(self._allocated_genes), requires_grad=True, device=device
        )

        optim = torch.optim.Adam((scale, rate, logits), lr=0.01)

        with Progressbar(it.count(1), leave=False, position=0) as iterator:
            running_rmse = None
            for epoch in iterator:
                previous_rmse = running_rmse
                for x in (
                    torch.cat(x["ST"]["data"]).to(device) for x in dataloader
                ):
                    distr = NegativeBinomial(
                        r2rp(scale) * r2rp(rate[self._gene_indices]),
                        logits=logits[self._gene_indices],
                    )
                    rmse = (
                        ((distr.mean - x) ** 2)
                        .mean(1)
                        .sqrt()
                        .mean()
                        .detach()
                        .cpu()
                    )
                    try:
                        running_rmse = running_rmse + 1e-2 * (
                            rmse - running_rmse
                        )
                    except TypeError:
                        running_rmse = rmse
                    iterator.set_description(
                        "Initializing global coefficients, please wait..."
                        + f" (RMSE: {running_rmse:.3f})"
                    )
                    optim.zero_grad()
                    nll = -distr.log_prob(x).sum()
                    nll.backward()
                    optim.step()
                if (epoch > 100) and (previous_rmse - running_rmse < 1e-4):
                    break

        self.__init_scale = r2rp(scale).detach().cpu()
        self.__init_rate = r2rp(rate).detach().cpu()
        self.__init_logits = logits.detach().cpu()

    def __init_scale_baseline(self):
        if self.__init_scale is None:
            self.__init_globals()
        return self.__init_scale

    def __init_rate_baseline(self):
        if self.__init_rate is None:
            self.__init_globals()
        return self.__init_rate

    def __init_logits_baseline(self):
        if self.__init_logits is None:
            self.__init_globals()
        return self.__init_logits

    def _get_scale_decoder(self, in_channels):
        # pylint: disable=no-self-use
        def _create_scale_decoder():
            dataset = require("dataloader").dataset
            decoder = torch.nn.Sequential(
                torch.nn.Conv2d(in_channels, in_channels, kernel_size=1),
                torch.nn.BatchNorm2d(in_channels, momentum=0.05),
                torch.nn.LeakyReLU(0.2, inplace=True),
                torch.nn.Conv2d(in_channels, 1, kernel_size=1),
                torch.nn.Softplus(),
            )
            torch.nn.init.normal_(decoder[-2].weight, std=1e-5)
            try:
                spot_size = estimate_spot_size(dataset)["ST"]
            except (NotImplementedError, KeyError):
                spot_size = 1.0
            decoder[-2].bias.data[...] = isoftplus(
                self.__init_scale_baseline() / spot_size
            )
            return decoder

        return get_module("scale", _create_scale_decoder, checkpoint=True)

    def _create_metagene_decoder(self, in_channels, n):
        decoder = torch.nn.Sequential(
            torch.nn.Conv2d(in_channels, 1, kernel_size=1)
        )
        torch.nn.init.constant_(decoder[-1].bias, self.__metagenes[n][0])
        return decoder

    def model(self, x, zs):
        # pylint: disable=too-many-locals, too-many-statements

        def _compute_rim(decoded):
            shared_representation = get_module(
                "metagene_shared",
                lambda: torch.nn.Sequential(
                    torch.nn.Conv2d(
                        decoded.shape[1], decoded.shape[1], kernel_size=1
                    ),
                    torch.nn.BatchNorm2d(decoded.shape[1], momentum=0.05),
                    torch.nn.LeakyReLU(0.2, inplace=True),
                ),
            )(decoded)
            rim = torch.cat(
                [
                    get_module(
                        f"decoder_{_encode_metagene_name(n)}",
                        partial(
                            self._create_metagene_decoder, decoded.shape[1], n
                        ),
                    )(shared_representation)
                    for n in self.metagenes
                ],
                dim=1,
            )
            rim = torch.nn.functional.softmax(rim, dim=1)
            return rim

        decoded = self._decode(zs)
        label = center_crop(x["label"], [None, *decoded.shape[-2:]])

        rim = checkpoint(_compute_rim, decoded)
        rim = center_crop(rim, [None, None, *label.shape[-2:]])
        rim = p.sample("rim", Delta(rim))

        scale = p.sample(
            "scale",
            Delta(
                center_crop(
                    self._get_scale_decoder(decoded.shape[1])(decoded),
                    [None, None, *label.shape[-2:]],
                )
            ),
        )
        rim = scale * rim

        with p.poutine.scale(
            scale=len(x["data"]) / max(self.n, len(x["data"]))
        ):
            rate_mg_prior = Normal(
                0.0,
                1e-8
                + get_param(
                    "rate_mg_prior_sd",
                    lambda: torch.ones(len(self._allocated_genes)),
                    constraint=constraints.positive,
                ),
            )
            rate_mg = torch.stack(
                [
                    p.sample(_encode_metagene_name(n), rate_mg_prior)
                    for n in self.metagenes
                ]
            )
            rate_mg = p.sample("rate_mg", Delta(rate_mg))
            rate_mg = rate_mg[:, self._gene_indices]

            rate_g_effects_baseline = get_param(
                "rate_g_effects_baseline",
                lambda: self.__init_rate_baseline().log(),
                lr_multiplier=5.0,
            )
            logits_g_effects_baseline = get_param(
                "logits_g_effects_baseline",
                # pylint: disable=unnecessary-lambda
                self.__init_logits_baseline,
                lr_multiplier=5.0,
            )
            rate_g_effects_prior = Normal(
                0.0,
                1e-8
                + get_param(
                    "rate_g_effects_prior_sd",
                    lambda: torch.ones(len(self._allocated_genes)),
                    constraint=constraints.positive,
                ),
            )
            rate_g_effects = p.sample("rate_g_effects", rate_g_effects_prior)
            rate_g_effects = torch.cat(
                [rate_g_effects_baseline.unsqueeze(0), rate_g_effects]
            )
            rate_g_effects = rate_g_effects[:, self._gene_indices]
            logits_g_effects_prior = Normal(
                0.0,
                1e-8
                + get_param(
                    "logits_g_effects_prior_sd",
                    lambda: torch.ones(len(self._allocated_genes)),
                    constraint=constraints.positive,
                ),
            )
            logits_g_effects = p.sample(
                "logits_g_effects", logits_g_effects_prior,
            )
            logits_g_effects = torch.cat(
                [logits_g_effects_baseline.unsqueeze(0), logits_g_effects]
            )
            logits_g_effects = logits_g_effects[:, self._gene_indices]

        effects = []
        for covariate, vals in require("covariates"):
            effect = p.sample(
                f"effect-{covariate}",
                OneHotCategorical(
                    to_device(torch.ones(len(vals))) / len(vals)
                ),
            )
            effects.append(effect)
        effects = torch.cat(
            [to_device(torch.ones(x["effects"].shape[0], 1)), *effects,], 1,
        ).float()

        logits_g = effects @ logits_g_effects
        rate_g = effects @ rate_g_effects
        rate_mg = rate_g[:, None] + rate_mg

        with scope(prefix=self.tag):
            image_distr = self._sample_image(x, decoded)

            for i, (data, label, rim, rate_mg, logits_g) in enumerate(
                zip(x["data"], label, rim, rate_mg, logits_g)
            ):
                zero_count_idxs = 1 + torch.where(data.sum(1) == 0)[0]
                partial_idxs = np.unique(
                    torch.cat([label[0], label[-1], label[:, 0], label[:, -1]])
                    .cpu()
                    .numpy()
                )
                partial_idxs = np.setdiff1d(
                    partial_idxs, zero_count_idxs.cpu().numpy()
                )
                mask = np.invert(
                    np.isin(label.cpu().numpy(), [0, *partial_idxs])
                )
                mask = torch.as_tensor(mask, device=label.device)

                if not mask.any():
                    continue

                label = label[mask]
                idxs, label = torch.unique(label, return_inverse=True)
                data = data[idxs - 1]
                p.sample(f"idx-{i}", Delta(idxs.float()))

                rim = rim[:, mask]
                labelonehot = sparseonehot(label)
                rim = torch.sparse.mm(labelonehot.t().float(), rim.t())
                rsg = rim @ rate_mg.exp()

                expression_distr = NegativeBinomial(
                    total_count=1e-8 + rsg, logits=logits_g
                )
                p.sample(f"xsg-{i}", expression_distr, obs=data)

        return image_distr, expression_distr

    def _sample_globals(self):
        dataset = require("dataloader").dataset
        device = get("default_device")

        p.sample(
            "rate_g_effects",
            Normal(
                get_param(
                    "rate_g_effects_mu",
                    lambda: torch.zeros(
                        dataset.data.design.shape[0],
                        len(self._allocated_genes),
                        device=device,
                    ),
                ),
                1e-8
                + get_param(
                    "rate_g_effects_sd",
                    lambda: 1e-2
                    * torch.ones(
                        dataset.data.design.shape[0],
                        len(self._allocated_genes),
                        device=device,
                    ),
                    constraint=constraints.positive,
                ),
            ),
            infer={"is_global": True},
        )

        p.sample(
            "logits_g_effects",
            Normal(
                get_param(
                    "logits_g_effects_mu",
                    lambda: torch.zeros(
                        dataset.data.design.shape[0],
                        len(self._allocated_genes),
                        device=device,
                    ),
                ),
                1e-8
                + get_param(
                    "logits_g_effects_sd",
                    lambda: 1e-2
                    * torch.ones(
                        dataset.data.design.shape[0],
                        len(self._allocated_genes),
                        device=device,
                    ),
                    constraint=constraints.positive,
                ),
            ),
            infer={"is_global": True},
        )

        # Sample metagene profiles
        def _sample_metagene(metagene, name):
            mu = get_param(
                f"{_encode_metagene_name(name)}_mu",
                # pylint: disable=unnecessary-lambda
                lambda: metagene.profile.float(),
                lr_multiplier=2.0,
            )
            sd = get_param(
                f"{_encode_metagene_name(name)}_sd",
                lambda: 1e-2
                * torch.ones_like(metagene.profile, device=device).float(),
                constraint=constraints.positive,
                lr_multiplier=2.0,
            )
            if len(self.__metagenes) < 2:
                mu = mu.detach()
                sd = sd.detach()
            p.sample(
                _encode_metagene_name(name),
                Normal(mu, 1e-8 + sd),
                infer={"is_global": True},
            )

        for name, metagene in self.metagenes.items():
            if metagene.profile is None:
                metagene = MetageneDefault(
                    metagene.scale, torch.zeros(len(self._allocated_genes))
                )
            _sample_metagene(metagene, name)

    def guide(self, x):
        with p.poutine.scale(
            scale=len(x["data"]) / max(self.n, len(x["data"]))
        ):
            self._sample_globals()
        for covariate, _ in require("covariates"):
            is_observed = x["effects"][covariate].values.any(1)
            effect_distr = RelaxedOneHotCategoricalStraightThrough(
                temperature=to_device(torch.as_tensor(0.1)),
                logits=torch.stack(
                    [
                        get_param(
                            f"effect-{covariate}-{sample}-logits",
                            torch.zeros(len(vals)),
                        )
                        for sample, vals in x["effects"][covariate].iterrows()
                    ]
                ),
            )
            with p.poutine.mask(mask=~to_device(torch.as_tensor(is_observed))):
                effect = p.sample(f"effect-{covariate}-all", effect_distr)
            effect[is_observed] = torch.as_tensor(
                x["effects"][covariate].values[is_observed]
            ).to(effect)
            p.sample(f"effect-{covariate}", Delta(effect))
        return super().guide(x)
