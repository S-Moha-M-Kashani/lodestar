from lodestar_brain.tools.websearch import make_search_tool


class StubSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, max_results=5):
        self.calls.append((query, max_results))
        return [{'title': 'Leiden algorithm', 'url': 'https://x.test/leiden',
                 'snippet': 'community detection'}]


def test_web_search_tool_delegates_to_provider():
    provider = StubSearch()
    tool = make_search_tool(provider)
    assert tool.name == 'web_search'
    out = tool.run({'query': 'leiden clustering', 'max_results': 3})
    assert provider.calls == [('leiden clustering', 3)]
    assert out[0]['url'] == 'https://x.test/leiden'


def test_web_search_spec():
    spec = make_search_tool(StubSearch()).spec()
    assert spec['function']['parameters']['required'] == ['query']
