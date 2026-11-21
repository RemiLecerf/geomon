"""Datasets: how each one is followed for the current site, and the actions asked from the page
(run by the scheduler, see geomon.datasets)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import datasets
from ..config import settings

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


def _check(code: str) -> datasets.Dataset:
    d = datasets.DATASETS.get(code)
    if d is None:
        raise HTTPException(404, f"Unknown dataset {code}")
    return d


@router.get("")
def list_datasets() -> dict:
    """Every dataset of the current site: mode, last update, stored size, pending actions, what it needs."""
    return {"site": settings.site_code, "categories": datasets.CATEGORIES, "modes": datasets.MODES,
            "datasets": datasets.status(settings.site_code)}


class Policy(BaseModel):
    mode: str
    keep_hours: int | None = None


@router.put("/{code}")
def set_mode(code: str, p: Policy) -> dict:
    """Chooses how a dataset is followed for the current site: update, frozen or live."""
    _check(code)
    try:
        return datasets.set_policy(settings.site_code, code, p.mode, p.keep_hours)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{code}/update")
def update_now(code: str) -> dict:
    """Asks the scheduler for one update now (whatever the mode: in frozen mode, with nothing stored,
    this downloads the archive once)."""
    _check(code)
    return {"request": datasets.request(settings.site_code, code, "update")}


@router.post("/{code}/delete")
def delete_data(code: str, confirm: str = "") -> dict:
    """Asks the scheduler to delete the stored data of a dataset for the current site; ``confirm`` must
    repeat the dataset code. The mode is set to frozen, so that the data does not come back."""
    d = _check(code)
    if confirm != code:
        raise HTTPException(422, "confirm must repeat the dataset code")
    if d.shared:
        raise HTTPException(422, f"{d.label} is shared by the sites: it is not deleted per site")
    if "frozen" in d.modes:
        datasets.set_policy(settings.site_code, code, "frozen")
    try:
        return {"request": datasets.request(settings.site_code, code, "delete")}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
