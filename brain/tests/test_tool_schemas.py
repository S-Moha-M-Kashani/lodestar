"""The tool schemas are API.

The old hand-written JSON Schema sat 40 lines below the function it described
and nothing checked that the two agreed — a new parameter could be added and
never reach the model. Deriving the schema from an explicit Pydantic args model
makes that drift impossible; these assertions are what keeps the *names* and
*enums* from drifting instead, the way the CSS class names are pinned for the
e2e suite.

Tools are built straight from the four factories with fakes, so all six are
present regardless of whether Chroma is configured — in create_app, recall_chat
is conditional on it.
"""
import numpy as np

from lodestar_brain.rag.chat_memory import make_recall_tool
from lodestar_brain.rag.index import LeidenIndex, make_retrieve_tool
from lodestar_brain.tools.board import COLUMNS, TYPES, make_board_tools
from lodestar_brain.tools.websearch import make_search_tool

EXPECTED = {'list_questions', 'create_question', 'update_question',
            'web_search', 'find_related', 'recall_chat'}


class FakeEmbedder:
    def embed(self, texts):
        return np.zeros((len(texts), 3))


class FakeSearch:
    def search(self, query, max_results=5):
        return []


class FakeMemory:
    def search(self, text, k=5):
        return []


def tools_by_name():
    # The clients are None on purpose: building a tool must not talk to
    # anything, and nothing here calls one.
    index = LeidenIndex(FakeEmbedder())
    tools = [*make_board_tools(None), make_search_tool(FakeSearch()),
             make_retrieve_tool(index, None), make_recall_tool(FakeMemory())]
    return {t.name: t for t in tools}


def _schema(tool):
    return tool.args_schema.model_json_schema()


def _enum(tool, field):
    """Pydantic inlines a Literal's enum ($defs stays empty) and wraps an
    optional one in anyOf with no $ref — handle both shapes."""
    schema = _schema(tool)
    prop = schema['properties'][field]
    for candidate in [prop, *prop.get('anyOf', [])]:
        if 'enum' in candidate:
            return candidate['enum']
        ref = candidate.get('$ref')
        if ref:
            return schema['$defs'][ref.split('/')[-1]]['enum']
    raise AssertionError(f'no enum on {field}')


def test_the_six_tool_names_are_exactly_these():
    assert set(tools_by_name()) == EXPECTED


def test_every_tool_has_a_non_empty_description():
    for name, tool in tools_by_name().items():
        assert tool.description.strip(), name


def test_every_tool_exposes_an_args_schema():
    for name, tool in tools_by_name().items():
        assert tool.args_schema is not None, name


def test_the_column_enum_is_the_boards_three_columns():
    tools = tools_by_name()
    assert COLUMNS == ['inbox', 'in-progress', 'answered']
    assert _enum(tools['create_question'], 'column_id') == COLUMNS
    assert _enum(tools['update_question'], 'column_id') == COLUMNS


def test_the_card_type_enum_is_the_boards_five_types():
    tools = tools_by_name()
    assert TYPES == ['question', 'problem', 'task', 'idea', 'plan']
    assert _enum(tools['create_question'], 'type') == TYPES
    assert _enum(tools['update_question'], 'type') == TYPES


def test_importance_and_urgency_keep_their_three_way_enum():
    update = tools_by_name()['update_question']
    assert _enum(update, 'importance') == ['high', 'low', '']
    assert _enum(update, 'urgency') == ['high', 'low', '']


def test_list_questions_still_accepts_an_empty_column_filter():
    # The old schema let the model omit the filter; '' has to stay legal or a
    # model that passes it explicitly gets a validation error where it used to
    # get the unfiltered board.
    assert '' in _enum(tools_by_name()['list_questions'], 'column_id')


def test_required_fields_match_the_old_hand_written_schemas():
    schemas = {name: _schema(tool) for name, tool in tools_by_name().items()}
    assert schemas['create_question']['required'] == ['title']
    assert schemas['update_question']['required'] == ['id']
    assert schemas['web_search']['required'] == ['query']
    assert schemas['find_related']['required'] == ['text']
    assert schemas['recall_chat']['required'] == ['text']
    assert schemas['list_questions'].get('required', []) == []


def test_create_question_still_tells_the_model_it_needs_approval():
    described = tools_by_name()['create_question'].description.lower()
    assert 'propos' in described or 'approv' in described or 'confirm' in described


def test_category_stays_a_free_string_with_guidance():
    # Categories are the user's own registry, so this must not become an enum;
    # the description is how the model learns what ids are in use.
    schema = _schema(tools_by_name()['create_question'])
    category = schema['properties']['category']
    assert category['type'] == 'string'
    assert 'enum' not in category
    assert 'registry' in category['description']
