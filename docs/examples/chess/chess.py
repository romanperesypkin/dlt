import os
from reretry import retry
from typing import Any, Iterator

import dlt
import requests

from dlt.common.typing import StrAny, TDataItems


@dlt.source
def chess(chess_url: str = dlt.config.value, title: str = "GM", max_players: int = 2, year: int = 2022, month: int = 10) -> Any:

    @retry(tries=10, delay=1, backoff=1.1)
    def _get_data_with_retry(path: str) -> StrAny:
        r = requests.get(f"{chess_url}{path}")
        r.raise_for_status()
        return r.json()  # type: ignore

    @dlt.resource(write_disposition="replace")
    def players() -> Iterator[TDataItems]:
        # return players one by one, you could also return a list that would be faster but we want to pass players item by item to the transformer
        for p in _get_data_with_retry(f"titled/{title}")["players"][:max_players]:
            yield p

    # this resource takes data from players and returns profiles
    @dlt.transformer(data_from=players, write_disposition="replace")
    def players_profiles(username: Any) -> Iterator[TDataItems]:
        yield _get_data_with_retry(f"player/{username}")

    # this resource takes data from players and returns games for the last month if not specified otherwise
    @dlt.transformer(data_from=players, write_disposition="append")
    def players_games(username: Any) -> Iterator[TDataItems]:
        # https://api.chess.com/pub/player/{username}/games/{YYYY}/{MM}
        path = f"player/{username}/games/{year:04d}/{month:02d}"
        yield _get_data_with_retry(path)["games"]

    return players(), players_profiles, players_games

if __name__ == "__main__":
    print("You must run this from the docs/examples/chess folder")
    assert os.getcwd().endswith("chess")
    # chess_url in config.toml, credentials for postgres in secrets.toml, credentials always under credentials key
    # mind the full_refresh: it makes the pipeline to load to a distinct dataset each time it is run and always is resetting the schema and state
    info = dlt.pipeline(
        pipeline_name="chess_games",
        destination="postgres",
        dataset_name="chess",
        full_refresh=True
    ).run(
        chess(max_players=5, month=9)
    )
    # display where the data went
    print(info)