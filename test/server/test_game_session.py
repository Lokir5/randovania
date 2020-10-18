import contextlib
import dataclasses
import datetime
import json
from unittest.mock import MagicMock, PropertyMock, patch, call

import peewee
import pytest

from randovania.game_description.assignment import PickupTarget
from randovania.game_description.item.item_category import ItemCategory
from randovania.game_description.resources.pickup_entry import PickupEntry, ConditionalResources
from randovania.interface_common.cosmetic_patches import CosmeticPatches
from randovania.interface_common.players_configuration import PlayersConfiguration
from randovania.layout.preset_migration import VersionedPreset
from randovania.network_common.admin_actions import SessionAdminUserAction, SessionAdminGlobalAction
from randovania.network_common.error import InvalidAction
from randovania.network_common.session_state import GameSessionState
from randovania.server import game_session, database


@pytest.fixture(name="mock_emit_session_update")
def _mock_emit_session_update(mocker) -> MagicMock:
    return mocker.patch("randovania.server.game_session._emit_session_update", autospec=True)


def test_setup_app():
    game_session.setup_app(MagicMock())


def test_list_game_sessions(clean_database):
    # Setup
    someone = database.User.create(name="Someone")
    database.GameSession.create(name="Debug", num_teams=1, creator=someone)
    database.GameSession.create(name="Other", num_teams=2, creator=someone)
    state = GameSessionState.SETUP.value

    # Run
    result = game_session.list_game_sessions(MagicMock())

    # Assert
    assert result == [
        {'has_password': False, 'id': 1, 'state': state, 'name': 'Debug', 'num_players': 0, 'creator': 'Someone'},
        {'has_password': False, 'id': 2, 'state': state, 'name': 'Other', 'num_players': 0, 'creator': 'Someone'},
    ]


def test_create_game_session(clean_database, preset_manager):
    # Setup
    user = database.User.create(id=1234, discord_id=5678, name="The Name")
    sio = MagicMock()
    sio.get_current_user.return_value = user

    # Run
    result = game_session.create_game_session(sio, "My Room")

    # Assert
    session = database.GameSession.get(1)
    assert session.name == "My Room"
    assert result == {
        'id': 1,
        'state': GameSessionState.SETUP.value,
        'name': 'My Room',
        'players': [{'admin': True, 'id': 1234, 'name': 'The Name', 'row': 0}],
        'presets': [preset_manager.default_preset.as_json],
        'actions': [],
        'seed_hash': None,
        'spoiler': None,
        'word_hash': None,
        'permalink': None,
        'generation_in_progress': None,
    }


def test_join_game_session(mock_emit_session_update: MagicMock,
                           clean_database):
    # Setup
    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Other Name")
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    session = database.GameSession.create(name="The Session", password=None, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionMembership.create(user=user2, session=session, row=0, admin=True)

    # Run
    result = game_session.join_game_session(sio, 1, None)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert result == {
        'id': 1,
        'state': GameSessionState.SETUP.value,
        'name': 'The Session',
        'players': [
            {'admin': True, 'id': 1235, 'name': 'Other Name', 'row': 0},
            {'admin': False, 'id': 1234, 'name': 'The Name', 'row': None},
        ],
        'actions': [],
        'presets': [{}],
        'seed_hash': None,
        'spoiler': None,
        'word_hash': None,
        'permalink': None,
        'generation_in_progress': None,
    }


def test_game_session_request_pickups_missing_membership(clean_database):
    with pytest.raises(peewee.DoesNotExist):
        game_session.game_session_request_pickups(MagicMock(), 1)


def test_game_session_request_pickups_not_in_game(flask_app, clean_database):
    # Setup
    user = database.User.create(id=1234, discord_id=5678, name="The Name")
    session = database.GameSession.create(name="Debug", creator=user)
    database.GameSessionMembership.create(user=user, session=session, row=0, admin=False)

    sio = MagicMock()
    sio.get_current_user.return_value = user

    # Run
    result = game_session.game_session_request_pickups(sio, 1)

    # Assert
    assert result == []


@pytest.fixture(name="two_player_session")
def two_player_session_fixture(clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Other Name")

    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionPreset.create(session=session, row=1, preset="{}")

    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=False)
    database.GameSessionMembership.create(user=user2, session=session, row=1, admin=False)
    database.GameSessionTeamAction.create(session=session, provider_row=1, provider_location_index=0, receiver_row=0)

    return session


@patch("randovania.server.game_session._get_pickup_target", autospec=True)
@patch("randovania.server.game_session._get_resource_database", autospec=True)
@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_request_pickups_one_action(mock_session_description: PropertyMock,
                                                 mock_get_resource_database: MagicMock,
                                                 mock_get_pickup_target: MagicMock,
                                                 flask_app, two_player_session, echoes_resource_database):
    # Setup
    sio = MagicMock()
    sio.get_current_user.return_value = database.User.get_by_id(1234)

    pickup = PickupEntry("A", 1, ItemCategory.TEMPLE_KEY, ItemCategory.KEY,
                         (
                             ConditionalResources(None, None, ((echoes_resource_database.item[0], 1),)),
                         ))
    mock_get_pickup_target.return_value = PickupTarget(pickup=pickup, player=0)
    mock_get_resource_database.return_value = echoes_resource_database

    # Run
    result = game_session.game_session_request_pickups(sio, 1)

    # Assert
    mock_get_resource_database.assert_called_once_with(mock_session_description.return_value, 0)
    mock_get_pickup_target.assert_called_once_with(mock_session_description.return_value, 1, 0)
    assert result == [{'message': 'Received A from Other Name', 'pickup': '0n)%A0Du'}]


@patch("flask_socketio.emit", autospec=True)
@patch("randovania.server.game_session._get_pickup_target", autospec=True)
@patch("randovania.server.game_session._get_resource_database", autospec=True)
@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_collect_pickup_for_self(mock_session_description: PropertyMock,
                                              mock_get_resource_database: MagicMock,
                                              mock_get_pickup_target: MagicMock,
                                              mock_emit: MagicMock,
                                              flask_app, two_player_session, echoes_resource_database):
    sio = MagicMock()
    sio.get_current_user.return_value = database.User.get_by_id(1234)

    pickup = PickupEntry("A", 1, ItemCategory.TEMPLE_KEY, ItemCategory.KEY,
                         (
                             ConditionalResources(None, None, ((echoes_resource_database.item[0], 1),)),
                         ))
    mock_get_resource_database.return_value = echoes_resource_database
    mock_get_pickup_target.return_value = PickupTarget(pickup, 0)

    # Run
    with flask_app.test_request_context():
        result = game_session.game_session_collect_locations(sio, 1, (0,))

    # Assert
    assert result is None
    mock_emit.assert_not_called()
    mock_get_pickup_target.assert_called_once_with(mock_session_description.return_value, 0, 0)
    with pytest.raises(peewee.DoesNotExist):
        database.GameSessionTeamAction.get(session=two_player_session, provider_row=0,
                                           provider_location_index=0)


@patch("flask_socketio.emit", autospec=True)
@patch("randovania.server.game_session._get_pickup_target", autospec=True)
@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_collect_pickup_etm(mock_session_description: PropertyMock,
                                         mock_get_pickup_target: MagicMock,
                                         mock_emit: MagicMock,
                                         flask_app, two_player_session, echoes_resource_database):
    sio = MagicMock()
    sio.get_current_user.return_value = database.User.get_by_id(1234)

    mock_get_pickup_target.return_value = None

    # Run
    with flask_app.test_request_context():
        result = game_session.game_session_collect_locations(sio, 1, (0,))

    # Assert
    assert result is None
    mock_emit.assert_not_called()
    mock_get_pickup_target.assert_called_once_with(mock_session_description.return_value, 0, 0)
    with pytest.raises(peewee.DoesNotExist):
        database.GameSessionTeamAction.get(session=two_player_session, provider_row=0,
                                           provider_location_index=0)


@pytest.mark.parametrize(("locations_to_collect", "exists"), [
    ((0,), ()),
    ((0,), (0,)),
    ((0, 1), ()),
    ((0, 1), (0,)),
    ((0, 1), (0, 1)),
])
def test_game_session_collect_pickup_other(flask_app, two_player_session, echoes_resource_database,
                                           locations_to_collect, exists, mock_emit_session_update, mocker):
    mock_emit: MagicMock = mocker.patch("flask_socketio.emit", autospec=True)
    mock_get_pickup_target: MagicMock = mocker.patch("randovania.server.game_session._get_pickup_target", autospec=True)
    mock_session_description: PropertyMock = mocker.patch("randovania.server.database.GameSession.layout_description",
                                                          new_callable=PropertyMock)

    sio = MagicMock()
    sio.get_current_user.return_value = database.User.get_by_id(1234)
    mock_get_pickup_target.return_value = PickupTarget(MagicMock(), 1)

    for existing_id in exists:
        database.GameSessionTeamAction.create(session=two_player_session, provider_row=0,
                                              provider_location_index=existing_id, receiver_row=0)

    # Run
    with flask_app.test_request_context():
        result = game_session.game_session_collect_locations(sio, 1, locations_to_collect)

    # Assert
    assert result is None
    mock_get_pickup_target.assert_has_calls([
        call(mock_session_description.return_value, 0, location)
        for location in locations_to_collect
    ])
    for location in locations_to_collect:
        database.GameSessionTeamAction.get(session=two_player_session, provider_row=0,
                                           provider_location_index=location)
    if exists == locations_to_collect:
        mock_emit.assert_not_called()
        mock_emit_session_update.assert_not_called()
    else:
        mock_emit.assert_called_once_with("game_has_update", {"session": 1, "row": 1, },
                                          room=f"game-session-1-1235")
        mock_emit_session_update.assert_called_once_with(database.GameSession.get(id=1))


@pytest.mark.parametrize("is_observer", [False, True])
def test_game_session_admin_player_switch_is_observer(clean_database, flask_app, mock_emit_session_update, is_observer):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=None if is_observer else 0, admin=False)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    with flask_app.test_request_context():
        game_session.game_session_admin_player(sio, 1, 1234, SessionAdminUserAction.SWITCH_IS_OBSERVER.value, None)

    # Assert
    membership = database.GameSessionMembership.get(user=user1, session=session)
    assert membership.is_observer != is_observer
    if is_observer:
        assert membership.row == 0
    mock_emit_session_update.assert_called_once_with(database.GameSession.get(id=1))


def test_game_session_admin_kick_last(clean_database, mocker):
    mock_emit = mocker.patch("flask_socketio.emit")

    user = database.User.create(id=1234, discord_id=5678, name="The Name")
    sio = MagicMock()
    sio.get_current_user.return_value = user
    game_session.create_game_session(sio, "My Room")
    session = database.GameSession.get_by_id(1)
    database.GameSessionTeamAction.create(session=session, provider_row=0, provider_location_index=0, receiver_row=0,
                                          time=datetime.datetime(2020, 5, 2, 10, 20, tzinfo=datetime.timezone.utc))

    # Run
    game_session.game_session_admin_player(sio, 1, 1234, SessionAdminUserAction.KICK.value, None)

    # Assert
    for table in [database.GameSession, database.GameSessionPreset,
                  database.GameSessionMembership, database.GameSessionTeamAction]:
        assert list(table.select()) == []
    assert database.User.get_by_id(1234) == user

    mock_emit.assert_called_once_with(
        'game_session_update',
        {'id': 1, 'name': 'My Room', 'state': 'setup', 'players': [], 'presets': [], 'actions': [],
         'spoiler': None, 'word_hash': None, 'seed_hash': None, 'permalink': None, 'generation_in_progress': None},
        room='game-session-1')


@pytest.mark.parametrize("offset", [-1, 1])
def test_game_session_admin_player_move(clean_database, flask_app, mock_emit_session_update, offset: int):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionPreset.create(session=session, row=1, preset="{}")
    database.GameSessionPreset.create(session=session, row=2, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=1, admin=False)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    with flask_app.test_request_context():
        game_session.game_session_admin_player(sio, 1, 1234, "move", offset)

    # Assert
    membership = database.GameSessionMembership.get(user=user1, session=session)
    assert membership.row == 1 + offset
    mock_emit_session_update.assert_called_once_with(database.GameSession.get(id=1))


@patch("randovania.games.prime.patcher_file.create_patcher_file", autospec=True)
@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_admin_player_patcher_file(mock_layout_description: PropertyMock,
                                                mock_create_patcher_file: MagicMock,
                                                clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Brother")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionPreset.create(session=session, row=1, preset="{}")
    database.GameSessionPreset.create(session=session, row=2, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=2, admin=False)
    database.GameSessionMembership.create(user=user2, session=session, row=1, admin=False)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    cosmetic = CosmeticPatches(open_map=False)

    # Run
    result = game_session.game_session_admin_player(sio, 1, 1234, "create_patcher_file", cosmetic.as_json)

    # Assert
    mock_create_patcher_file.assert_called_once_with(
        mock_layout_description.return_value,
        PlayersConfiguration(2, {
            0: "Player 1",
            1: "Brother",
            2: "The Name",
        }),
        cosmetic
    )
    assert result is mock_create_patcher_file.return_value


def test_game_session_admin_session_delete_session(mock_emit_session_update: MagicMock, clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.DELETE_SESSION.value, None)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert list(database.GameSession.select()) == []


def test_game_session_admin_session_create_row(mock_emit_session_update: MagicMock,
                                               clean_database, preset_manager):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    game_session.game_session_admin_session(sio, 1, "create_row", preset_manager.default_preset.as_json)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).num_rows == 1


def test_game_session_admin_session_change_row(mock_emit_session_update: MagicMock,
                                               clean_database, preset_manager):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionPreset.create(session=session, row=1, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Make sure the preset is using the latest version
    preset_manager.default_preset.ensure_converted()

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.CHANGE_ROW.value,
                                            (1, preset_manager.default_preset.as_json))

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    new_preset_row = database.GameSessionPreset.get(database.GameSessionPreset.session == session,
                                                    database.GameSessionPreset.row == 1)
    assert json.loads(new_preset_row.preset) == preset_manager.default_preset.as_json


def test_game_session_admin_session_delete_row(mock_emit_session_update: MagicMock,
                                               clean_database, preset_manager):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionPreset.create(session=session, row=1, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.DELETE_ROW.value, 1)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).num_rows == 1


@pytest.mark.parametrize("not_last_row", [False, True])
def test_game_session_admin_session_delete_row_invalid(mock_emit_session_update,
                                                       clean_database, preset_manager, not_last_row):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1
    if not_last_row:
        database.GameSessionPreset.create(session=session, row=1, preset="{}")
        expected_message = "Can only delete the last row"
        expected_num_rows = 2
    else:
        expected_message = "Can't delete row when there's only one"
        expected_num_rows = 1

    # Run
    with pytest.raises(InvalidAction) as e:
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.DELETE_ROW.value, 0)

    # Assert
    assert e.value.message == expected_message
    mock_emit_session_update.assert_not_called()
    assert database.GameSession.get_by_id(1).num_rows == expected_num_rows


@pytest.mark.parametrize("case", ["to_false", "to_true_free", "to_true_busy"])
def test_game_session_admin_session_update_layout_generation(mock_emit_session_update: MagicMock,
                                                             clean_database, case):
    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Other")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          generation_in_progress=user2 if case == "to_true_busy" else None)
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    if case == "to_true_busy":
        expectation = pytest.raises(InvalidAction, match="Generation already in progress by Other.")
        expected_user = user2
    else:
        expectation = contextlib.nullcontext()
        if case == "to_false":
            expected_user = None
        else:
            expected_user = user1

    # Run
    with expectation:
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.UPDATE_LAYOUT_GENERATION.value,
                                                case != "to_false")

    # Assert
    if case == "to_true_busy":
        mock_emit_session_update.assert_not_called()
    else:
        mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).generation_in_progress == expected_user


def test_game_session_admin_session_change_layout_description(clean_database, preset_manager, mock_emit_session_update,
                                                              mocker):
    mock_verify_no_layout_description = mocker.patch("randovania.server.game_session._verify_no_layout_description",
                                                     autospec=True)
    mock_from_json_dict: MagicMock = mocker.patch(
        "randovania.layout.layout_description.LayoutDescription.from_json_dict")

    preset_as_json = json.dumps(preset_manager.default_preset.as_json)
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          generation_in_progress=user1)
    database.GameSessionPreset.create(session=session, row=0, preset=preset_as_json)
    database.GameSessionPreset.create(session=session, row=1, preset=preset_as_json)
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)

    new_preset = preset_manager.default_preset.get_preset()
    new_preset = dataclasses.replace(new_preset,
                                     patcher_configuration=dataclasses.replace(new_preset.patcher_configuration,
                                                                               menu_mod=False))

    sio = MagicMock()
    sio.get_current_user.return_value = user1
    layout_description = mock_from_json_dict.return_value
    layout_description.as_json = "some_json_string"
    layout_description.permalink.player_count = 2
    layout_description.permalink.presets = {i: new_preset for i in (0, 1)}

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.CHANGE_LAYOUT_DESCRIPTION.value,
                                            "layout_description_json")

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    mock_verify_no_layout_description.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).layout_description_json == '"some_json_string"'
    assert database.GameSession.get_by_id(1).generation_in_progress is None

    new_session = database.GameSession.get_by_id(1)
    new_json = json.dumps(VersionedPreset.with_preset(new_preset).as_json)
    assert [preset.preset for preset in new_session.presets] == [new_json] * 2


def test_game_session_admin_session_remove_layout_description(mock_emit_session_update: MagicMock, clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          generation_in_progress=user1,
                                          layout_description_json="layout_description_json")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.CHANGE_LAYOUT_DESCRIPTION.value,
                                            None)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).layout_description_json is None
    assert database.GameSession.get_by_id(1).generation_in_progress is None


@pytest.mark.parametrize("other_user", [False, True])
def test_game_session_admin_session_change_layout_description_invalid(mock_emit_session_update: MagicMock,
                                                                      clean_database, other_user):
    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Other")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          generation_in_progress=user2 if other_user else None)
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    if other_user:
        expected_message = "Waiting for a layout from Other."
    else:
        expected_message = "Not waiting for a layout."

    # Run
    with pytest.raises(InvalidAction, match=expected_message):
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.CHANGE_LAYOUT_DESCRIPTION.value,
                                                "layout_description_json")

    # Assert
    mock_emit_session_update.assert_not_called()
    assert database.GameSession.get_by_id(1).layout_description_json is None


@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_admin_session_download_layout_description(mock_layout_description: PropertyMock,
                                                                clean_database, mock_emit_session_update):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          layout_description_json="layout_description_json")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=False)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    result = game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.DOWNLOAD_LAYOUT_DESCRIPTION.value,
                                                     None)

    # Assert
    mock_emit_session_update.assert_not_called()
    mock_layout_description.assert_called_once()
    assert result == database.GameSession.get_by_id(1).layout_description_json


@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_admin_session_download_layout_description_no_spoiler(mock_layout_description: PropertyMock,
                                                                           clean_database, mock_emit_session_update):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          layout_description_json="layout_description_json")
    database.GameSessionMembership.create(user=user1, session=session, row=None, admin=False)
    sio = MagicMock()
    sio.get_current_user.return_value = user1
    mock_layout_description.return_value.permalink.spoiler = False

    # Run
    with pytest.raises(InvalidAction):
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.DOWNLOAD_LAYOUT_DESCRIPTION.value,
                                                None)

    # Assert
    mock_emit_session_update.assert_not_called()
    mock_layout_description.assert_called_once()


@patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
def test_game_session_admin_session_start_session(mock_session_description: PropertyMock,
                                                  mock_emit_session_update,
                                                  clean_database, preset_manager):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1,
                                          layout_description_json="{}")
    database.GameSessionPreset.create(session=session, row=0, preset="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.START_SESSION.value, None)

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).state == GameSessionState.IN_PROGRESS


@pytest.mark.parametrize("starting_state", [GameSessionState.SETUP, GameSessionState.IN_PROGRESS,
                                            GameSessionState.FINISHED])
def test_game_session_admin_session_finish_session(clean_database, mock_emit_session_update, starting_state):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=starting_state, creator=user1)
    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1
    if starting_state != GameSessionState.IN_PROGRESS:
        expectation = pytest.raises(InvalidAction, match="Invalid Action: Session is not in progress")
    else:
        expectation = contextlib.nullcontext()

    # Run
    with expectation:
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.FINISH_SESSION.value, None)

    # Assert
    if starting_state != GameSessionState.IN_PROGRESS:
        mock_emit_session_update.assert_not_called()
        assert database.GameSession.get_by_id(1).state == starting_state
    else:
        mock_emit_session_update.assert_called_once_with(session)
        assert database.GameSession.get_by_id(1).state == GameSessionState.FINISHED


def test_game_session_admin_session_reset_session(clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1

    # Run
    with pytest.raises(InvalidAction):
        game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.RESET_SESSION.value, None)


def test_game_session_admin_session_change_password(clean_database, mock_emit_session_update):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.SETUP, creator=user1)
    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=True)
    sio = MagicMock()
    sio.get_current_user.return_value = user1
    expected_password = 'da92cfbc5e318c64e33dc1b0501e5db214cea0e2a5cecabf90269f32f8eaa15f'

    # Run
    game_session.game_session_admin_session(sio, 1, SessionAdminGlobalAction.CHANGE_PASSWORD.value, "the_password")

    # Assert
    mock_emit_session_update.assert_called_once_with(session)
    assert database.GameSession.get_by_id(1).password == expected_password


def test_change_row_missing_arguments():
    with pytest.raises(InvalidAction):
        game_session._change_row(MagicMock(), MagicMock(), (5,))


def test_verify_in_setup(clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1,
                                          layout_description_json="{}")

    with pytest.raises(InvalidAction):
        game_session._verify_in_setup(session)


def test_verify_no_layout_description(clean_database):
    user1 = database.User.create(id=1234, name="The Name")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1,
                                          layout_description_json="{}")

    with pytest.raises(InvalidAction):
        game_session._verify_in_setup(session)


def test_game_session_request_update(clean_database, mocker):
    mock_layout = mocker.patch("randovania.server.database.GameSession.layout_description", new_callable=PropertyMock)
    target = mock_layout.return_value.all_patches.__getitem__.return_value.pickup_assignment.__getitem__.return_value
    target.pickup.name = "The Pickup"
    mock_layout.return_value.shareable_word_hash = "Words of O-Lir"
    mock_layout.return_value.shareable_hash = "ABCDEFG"
    mock_layout.return_value.permalink.spoiler = True
    mock_layout.return_value.permalink.as_str = "<permalink>"

    user1 = database.User.create(id=1234, name="The Name")
    user2 = database.User.create(id=1235, name="Other")
    session = database.GameSession.create(id=1, name="Debug", state=GameSessionState.IN_PROGRESS, creator=user1,
                                          layout_description_json="{}")
    database.GameSessionMembership.create(user=user1, session=session, row=0, admin=True)
    database.GameSessionMembership.create(user=user2, session=session, row=1, admin=False)
    database.GameSessionTeamAction.create(session=session, provider_row=1, provider_location_index=0, receiver_row=0,
                                          time=datetime.datetime(2020, 5, 2, 10, 20, tzinfo=datetime.timezone.utc))

    # Run
    result = game_session.game_session_request_update(MagicMock(), 1)

    # Assert
    assert result == {
        "id": 1,
        "name": "Debug",
        "state": GameSessionState.IN_PROGRESS.value,
        "players": [
            {
                "id": 1234,
                "name": "The Name",
                "row": 0,
                "admin": True,
            },
            {
                "id": 1235,
                "name": "Other",
                "row": 1,
                "admin": False,
            },
        ],
        "presets": [],
        "actions": [
            {
                "message": "Other found The Pickup for The Name.",
                "time": "2020-05-02T10:20:00+00:00",
            }
        ],
        "spoiler": True,
        "word_hash": "Words of O-Lir",
        "seed_hash": "ABCDEFG",
        "permalink": "<permalink>",
        "generation_in_progress": None,
    }