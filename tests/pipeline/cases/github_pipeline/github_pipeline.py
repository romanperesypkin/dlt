import sys

import dlt

from dlt.common.typing import TDataItem
from dlt.common import json, pendulum


def convert_dates(item: TDataItem) -> TDataItem:
    item["created_at"] = pendulum.parse(item["created_at"])
    return item


@dlt.source(root_key=True)
def github():
    @dlt.resource(
        table_name="issues",
        write_disposition="merge",
        primary_key="id",
        merge_key=("node_id", "url"),
    )
    def load_issues(
        created_at=dlt.sources.incremental[pendulum.DateTime]("created_at"),  # noqa: B008
    ):
        # we should be in TEST_STORAGE folder
        with open(
            "../tests/normalize/cases/github.issues.load_page_5_duck.json", "r", encoding="utf-8"
        ) as f:
            issues = map(convert_dates, sorted(json.load(f), key=lambda x: x["created_at"]))
            yield from issues

    return load_issues


if __name__ == "__main__":
    p = dlt.pipeline(
        "dlt_github_pipeline", destination="duckdb", dataset_name="github_3", full_refresh=False
    )
    github_source = github()
    if len(sys.argv) > 1:
        # load only N issues
        limit = int(sys.argv[1])
        github_source.add_limit(limit)
    info = p.run(github_source)
    print(info)
