import asyncio

from app.models import Event
from app import sources


class Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Client:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json):
        self.calls.append((url, json))
        return Response([{
            "asset_id": item["token_id"], "timestamp": "1784563200000",
            "hash": f"hash-{item['token_id']}",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.51", "size": "1000"}],
        } for item in json])


def test_initial_books_use_documented_bulk_endpoint_in_500_token_batches(monkeypatch):
    Client.calls = []
    monkeypatch.setattr(sources.httpx, "AsyncClient", Client)
    event = Event("A vs B", "basketball", "A", "B")
    metadata = {
        f"token-{index}": {
            "market": "moneyline", "outcome": "A", "accepting_orders": True,
            "fee_rate": 0.0,
        }
        for index in range(501)
    }
    quotes = asyncio.run(sources._initial_polymarket_quotes(event, metadata))
    assert len(quotes) == 501
    assert [len(body) for _, body in Client.calls] == [500, 1]
    assert {url for url, _ in Client.calls} == {"https://clob.polymarket.com/books"}
