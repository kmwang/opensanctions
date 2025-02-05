from typing import List
from concurrent.futures import Future, wait, ThreadPoolExecutor

from opensanctions import settings
from opensanctions.core import Dataset, Context
from opensanctions.core.resolver import get_resolver
from opensanctions.core.statements import resolve_all_canonical
from opensanctions.exporters import export_metadata, export_dataset
from opensanctions.core.loader import Database
from opensanctions.core.db import engine_tx


def _compute_futures(futures: List[Future]):
    wait(futures)
    for future in futures:
        future.result()


def run_pipeline(
    scope_name: str,
    crawl: bool = True,
    export: bool = True,
    threads: int = settings.THREADS,
) -> None:
    scope = Dataset.require(scope_name)
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures: List[Future] = []
        if crawl is True:
            for source in scope.sources:
                ctx = Context(source)
                futures.append(executor.submit(ctx.crawl))
            _compute_futures(futures)

        if export is True:
            resolver = get_resolver()
            with engine_tx() as conn:
                resolve_all_canonical(conn, resolver)
            database = Database(scope, resolver, cached=True)
            database.view(scope)
            futures = []
            for dataset_ in scope.datasets:
                futures.append(executor.submit(export_dataset, dataset_, database))
            futures.append(executor.submit(export_metadata))
            _compute_futures(futures)


def run_enrich(scope_name: str, external_name: str, threshold: float):
    scope = Dataset.require(scope_name)
    external = Dataset.require(external_name)
    ctx = Context(external)
    resolver = get_resolver()
    database = Database(scope, resolver, cached=False)
    loader = database.view(scope)
    ctx.enrich(resolver, loader, threshold=threshold)
    resolver.save()
