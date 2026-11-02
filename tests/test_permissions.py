import pytest

from bot.permissions import Action, ControlContext, ControlError, check_control


def ctx(**kw):
    base = dict(
        is_owner=False,
        is_moderator=False,
        has_dj_role=False,
        user_channel_id=1,
        bot_channel_id=1,
        listeners=3,
        is_requester=False,
    )
    base.update(kw)
    return ControlContext(**base)


def test_must_be_in_voice():
    with pytest.raises(ControlError, match="Join a voice channel"):
        check_control(ctx(user_channel_id=None), "everyone", Action.ADD)


def test_must_share_channel():
    with pytest.raises(ControlError, match="<#1>"):
        check_control(ctx(user_channel_id=2), "everyone", Action.CONTROL)


def test_owner_bypasses_everything():
    check_control(ctx(is_owner=True, user_channel_id=None), "mods", Action.CONTROL)


def test_everyone_mode():
    check_control(ctx(), "everyone", Action.CONTROL)
    check_control(ctx(bot_channel_id=None), "everyone", Action.ADD)  # bot not connected yet


@pytest.mark.parametrize("mode", ["dj", "mods"])
def test_restricted_modes_still_allow_adding(mode):
    check_control(ctx(), mode, Action.ADD)


def test_dj_mode():
    with pytest.raises(ControlError, match="DJ"):
        check_control(ctx(), "dj", Action.CONTROL)
    check_control(ctx(has_dj_role=True), "dj", Action.CONTROL)
    check_control(ctx(is_moderator=True), "dj", Action.CONTROL)


def test_mods_mode_ignores_dj_role():
    with pytest.raises(ControlError, match="moderators"):
        check_control(ctx(has_dj_role=True), "mods", Action.CONTROL)
    check_control(ctx(is_moderator=True), "mods", Action.CONTROL)


def test_requester_can_skip_own_song():
    check_control(ctx(is_requester=True), "dj", Action.SKIP)
    with pytest.raises(ControlError):
        check_control(ctx(is_requester=True), "dj", Action.CONTROL)


def test_alone_with_bot_has_full_control():
    check_control(ctx(listeners=1), "mods", Action.CONTROL)
