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
