import sys
import types

from lodestar_brain.tools.websearch import make_search_tool


class StubSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, max_results=5):
        self.calls.append((query, max_results))
        return [{'title': 'Leiden algorithm', 'url': 'https://x.test/leiden',
                 'snippet': 'community detection'}]


# This is a unit test.
def test_web_search_tool_delegates_to_provider():
    provider = StubSearch()
    tool = make_search_tool(provider)
    assert tool.name == 'web_search'
    out = tool.run({'query': 'leiden clustering', 'max_results': 3})
    assert provider.calls == [('leiden clustering', 3)]
    assert out[0]['url'] == 'https://x.test/leiden'


# This is a unit test.
def test_web_search_schema():
    tool = make_search_tool(StubSearch())
    schema = tool.args_schema.model_json_schema()
    assert schema['required'] == ['query']
    assert 'max_results' in schema['properties']


# This is a unit test.
def test_ddgs_is_built_with_an_explicit_timeout(monkeypatch):
    """The ddgs library happens to default to a 5s timeout today; relying on
    that means a library upgrade can silently unbound a chat turn. The provider
    must pass its own `DDGS_TIMEOUT` explicitly, and this test reads it off the
    constructor of a stubbed module so no network is ever touched."""
    from lodestar_brain.tools.websearch import DDGS_TIMEOUT, DdgsSearch

    class RecordingDDGS:
        kwargs = None

        def __init__(self, **kwargs):
            type(self).kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=5):
            return [{'title': 't', 'href': 'https://x.test/t', 'body': 'b'}]

    stub = types.ModuleType('ddgs')
    stub.DDGS = RecordingDDGS
    monkeypatch.setitem(sys.modules, 'ddgs', stub)

    rows = DdgsSearch().search('anything')
    assert rows[0]['url'] == 'https://x.test/t'
    assert RecordingDDGS.kwargs == {'timeout': DDGS_TIMEOUT}
    assert DDGS_TIMEOUT > 0
