import os

from xfuse.analyze.gene_maps import _run_gene_maps_analysis
from xfuse.session import Session
from xfuse.session.items.work_dir import WorkDir


def test_run_gene_maps_analysis(pretrained_toy_model, toydata, tmp_path):
    with Session(
        model=pretrained_toy_model,
        genes=toydata.dataset.genes,
        dataloader=toydata,
        covariates=toydata.dataset.data.design.columns,
        work_dir=WorkDir(tmp_path),
    ):
        _run_gene_maps_analysis()

    for section in toydata.dataset.data.design.index:
        for gene in toydata.dataset.genes:
            assert os.path.exists(tmp_path / section / f"{gene}_mean.jpg")
            assert os.path.exists(tmp_path / section / f"{gene}_stdv.jpg")
