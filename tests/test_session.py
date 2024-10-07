import warnings
import pytest
import pytest_asyncio
import devtools


async def print_obj(obj):
    print(obj)


@pytest_asyncio.fixture(scope="function", loop_scope="function")
async def session(browser):
    with pytest.warns(devtools.protocol.ExperimentalFeatureWarning):
        session_browser = await browser.create_session()
        warnings.warn(
            "Creating new sessions on Browser() only works with some versions of Chrome, it is experimental.",
            devtools.protocol.ExperimentalFeatureWarning,
        )
    yield session_browser
    await browser.close_session(session_browser)


@pytest.mark.asyncio
async def test_send_command(session):
    response = await session.send_command("Target.getTargets")
    assert "result" in response and "targetInfos" in response["result"]


@pytest.mark.asyncio
async def test_subscribe_once(session):
    session.subscribe_once("Page.*")
    assert "Page.*" in session.subscriptions_futures


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe(session):
    session.subscribe("*", print_obj, True)
    assert "*" in session.subscriptions
    session.unsubscribe("*")
    assert "*" not in session.subscriptions
    session.subscribe("INVALID", print_obj, False)
    assert "INVALID" in session.subscriptions
    session.unsubscribe("INVALID")
    assert "INVALID" not in session.subscriptions
