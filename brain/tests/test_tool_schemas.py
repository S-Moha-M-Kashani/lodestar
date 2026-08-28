"""The tool schemas are API.

The old hand-written JSON Schema sat 40 lines below the function it described
and nothing checked that the two agreed — a new parameter could be added and
never reach the model. Deriving the schema from an explicit Pydantic args model
makes that drift impossible; these assertions are what keeps the *names* and
*enums* from drifting instead, the way the CSS class names are pinned for the
e2e suite.

Tools are built straight from the factories with fakes, so all eight are
present regardless of whether Chroma is configured — in create_app, recall_chat
is conditional on it.
"""
from lodestar_brain.retrieval import CardIndex, LexicalHashEmbeddings
from lodestar_brain.tools.board import COLUMNS, HABIT_FREQS, TYPES, make_board_tools
from lodestar_brain.tools.memory import make_memory_tool
from lodestar_brain.tools.recap import make_recap_tool
from lodestar_brain.tools.retrieve import make_recall_tool, make_retrieve_tool
from lodestar_brain.tools.websearch import make_search_tool

EXPECTED = {'list_cards', 'create_card', 'update_card',
            'web_search', 'find_related', 'recall_chat', 'daily_recap',
            # The agent's own scratch pad, and the only tool here that writes.
            # What it writes to is the checkpoint store — never a card, never
            # the chat record — and every write is a visible step.
            'remember_fact'}


class FakeSearch:
    def search(self, query, max_results=5):
        return []


class FakeMemory:
    def search(self, text, k=5, board_id=None):
        return []


def tools_by_name():
    # The clients are None on purpose: building a tool must not talk to
    # anything, and nothing here calls one.
    index = CardIndex(LexicalHashEmbeddings())
    tools = [*make_board_tools(None), make_search_tool(FakeSearch()),
             make_retrieve_tool(index, None), make_recall_tool(FakeMemory()),
             make_recap_tool(None), make_memory_tool()]
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


# This is a unit test.
def test_the_eight_tool_names_are_exactly_these():
    assert set(tools_by_name()) == EXPECTED


# This is a unit test.
def test_every_tool_has_a_non_empty_description():
    for name, tool in tools_by_name().items():
        assert tool.description.strip(), name


# This is a unit test.
def test_every_tool_exposes_an_args_schema():
    for name, tool in tools_by_name().items():
        assert tool.args_schema is not None, name


# This is a unit test.
def test_the_column_enum_is_the_boards_three_columns():
    tools = tools_by_name()
    assert COLUMNS == ['inbox', 'in-progress', 'answered']
    assert _enum(tools['create_card'], 'column_id') == COLUMNS
    assert _enum(tools['update_card'], 'column_id') == COLUMNS


# This is a unit test.
def test_the_card_type_enum_is_the_boards_five_types():
    tools = tools_by_name()
    # 'plan' was the sixth until 2026-08-28 and is now a date on every card.
    assert TYPES == ['question', 'problem', 'task', 'idea', 'habit']
    assert _enum(tools['create_card'], 'type') == TYPES
    assert _enum(tools['update_card'], 'type') == TYPES


# This is a unit test.
def test_both_dates_are_offered_where_a_card_is_made_and_edited():
    tools = tools_by_name()
    for name in ('create_card', 'update_card'):
        fields = tools[name].args_schema.model_json_schema()['properties']
        assert 'deadline' in fields and 'plan' in fields, name
        # The description has to teach the difference, or the model will file
        # one as the other. The plan's text says what it is *and* how it relates
        # to the deadline it must never pass.
        plan_help = fields['plan']['description'].lower()
        assert 'means to do it' in plan_help and 'deadline' in plan_help, name
        assert 'due' in fields['deadline']['description'].lower(), name


# This is a unit test.
def test_a_habits_cadence_is_offered_where_the_card_is_made():
    tools = tools_by_name()
    # '' is the sixth option: every non-habit card leaves the frequency unset.
    assert _enum(tools['create_card'], 'frequency') == HABIT_FREQS + ['']
    fields = tools['create_card'].args_schema.model_json_schema()['properties']
    assert 'times_per_period' in fields
    # The cadence is set when the card is made, not edited afterwards, so
    # update_card deliberately does not carry it.
    assert 'frequency' not in tools['update_card'].args_schema.model_json_schema()['properties']


# This is a unit test.
def test_importance_and_urgency_keep_their_three_way_enum():
    update = tools_by_name()['update_card']
    assert _enum(update, 'importance') == ['high', 'low', '']
    assert _enum(update, 'urgency') == ['high', 'low', '']


# This is a unit test.
def test_list_cards_still_accepts_an_empty_column_filter():
    # The old schema let the model omit the filter; '' has to stay legal or a
    # model that passes it explicitly gets a validation error where it used to
    # get the unfiltered board.
    assert '' in _enum(tools_by_name()['list_cards'], 'column_id')


# This is a unit test.
def test_required_fields_match_the_old_hand_written_schemas():
    schemas = {name: _schema(tool) for name, tool in tools_by_name().items()}
    assert schemas['create_card']['required'] == ['title']
    assert schemas['update_card']['required'] == ['id']
    assert schemas['web_search']['required'] == ['query']
    assert schemas['find_related']['required'] == ['text']
    assert schemas['recall_chat']['required'] == ['text']
    assert schemas['list_cards'].get('required', []) == []


# This is a unit test.
def test_create_card_still_tells_the_model_it_needs_approval():
    described = tools_by_name()['create_card'].description.lower()
    assert 'propos' in described or 'approv' in described or 'confirm' in described


# This is a unit test.
def test_category_stays_a_free_string_with_guidance():
    # Categories are the user's own registry, so this must not become an enum;
    # the description is how the model learns what ids are in use.
    schema = _schema(tools_by_name()['create_card'])
    category = schema['properties']['category']
    assert category['type'] == 'string'
    assert 'enum' not in category
    assert 'registry' in category['description']


# This is a unit test.
def test_daily_recap_reaches_a_bounded_multi_day_window():
    """'Recap the last 3 days' used to be structurally impossible — `day` was a
    two-value enum, so the tool topped out at two days across two calls and the
    model narrated the gap. `days` (1..7, default 1) is the reach; `day` stays
    the window's end anchor."""
    recap = tools_by_name()['daily_recap']
    assert _enum(recap, 'day') == ['yesterday', 'today']
    days = _schema(recap)['properties']['days']
    assert (days['minimum'], days['maximum'], days['default']) == (1, 7, 1)
