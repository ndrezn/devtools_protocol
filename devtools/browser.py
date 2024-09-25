import platform
import os
import sys
import subprocess
import tempfile
import warnings
import json
import asyncio
from threading import Thread
from collections import OrderedDict

from .pipe import Pipe
from .protocol import Protocol
from .target import Target
from .session import Session
from .tab import Tab
from .system import which_browser

from .pipe import PipeClosedError

default_path = which_browser() # probably handle this better

# BrowserProcess will be inherited by Browser. It contains all loop, context, and process logic.
# It is just meant to help organize the code, it is not inherited anywhere else.
class BrowserProcess():
    def _check_loop(self):
        if self.loop and isinstance(self.loop, asyncio.SelectorEventLoop):
            # I think using set_event_loop_policy is too invasive (is system wide)
            # and may not work in situations where a framework manually set SEL
            self.loop_hack = True

    def __init__(self, path=None, headless=True, loop=None, executor=None, debug=False, debug_browser=False):
        # Configuration
        self.headless = headless
        self.debug = debug
        self.loop_hack = False # subprocess needs weird stuff w/ SelectorEventLoop

        # Set up stderr
        if not debug_browser:  # false o None
            stderr = subprocess.DEVNULL
        elif debug_browser is True:
            stderr = None
        else:
            stderr = debug
        self._stderr = stderr

        # Set up temp dir
        if platform.system() != "Windows":
            self.temp_dir = tempfile.TemporaryDirectory()
        else:
            self.temp_dir = tempfile.TemporaryDirectory(
                delete=False, ignore_cleanup_errors=True
            )

        # Set up process env
        new_env = os.environ.copy()

        if not path:
            path = os.environ.get("BROWSER_PATH", None)
        if not path:
            path = default_path
        if path:
            new_env["BROWSER_PATH"] = path
        else:
            raise RuntimeError(
                "Could not find an acceptable browser. Please set environmental variable BROWSER_PATH or pass `path=/path/to/browser` into the Browser() constructor."
            )


        new_env["USER_DATA_DIR"] = str(self.temp_dir.name)

        if headless:
            new_env["HEADLESS"] = "--headless"  # unset if false

        self._env = new_env
        if self.debug:
            print("DEBUG REPORT:")
            print(f"BROWSER_PATH: {new_env['BROWSER_PATH']}")
            print(f"USER_DATA_DIR: {new_env['USER_DATA_DIR']}")

        # Defaults for loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except Exception:
                loop = False
        self.loop = loop
        self._check_loop()

        # State
        self.executor = executor

        # Compose Resources
        self.pipe = Pipe(debug=debug)

        if not self.loop:
            self._open()

    async def _check_session(self, response):
        session_id = response['params']['sessionId']
        del self.protocol.sessions[session_id]
        # we need to remove this from protocol

    # so we just use one inside the other
    def __aenter__(self):
        if self.loop is True:
            self.loop = asyncio.get_running_loop()
            self._check_loop()
        self.future_self = self.loop.create_future()
        self.loop.create_task(self._open_async())
        self.browser.subscribe("Target.detachedFromTarget", self._check_session, repeating=True)
        self.protocol.run_read_loop()
        return self.future_self

    # await is basically the second part of __init__() if the user uses
    # await Browser(), which if they are using a loop, they need to.
    def __await__(self):
        return self.__aenter__().__await__()


    def _open(self):
        stderr = self._stderr
        env = self._env
        if platform.system() != "Windows":
            self.subprocess = subprocess.Popen(
                [
                    sys.executable,
                    os.path.join(
                        os.path.dirname(os.path.realpath(__file__)), "chrome_wrapper.py"
                    ),
                ],
                close_fds=True,
                stdin=self.pipe.read_to_chromium,
                stdout=self.pipe.write_from_chromium,
                stderr=stderr,
                env=env,
            )
        else:
            from .chrome_wrapper import open_browser
            self.subprocess = open_browser(to_chromium=self.pipe.read_to_chromium,
                                                   from_chromium=self.pipe.write_from_chromium,
                                                   stderr=stderr,
                                                   env=env,
                                                   loop_hack=self.loop_hack)


    async def _open_async(self):
        stderr = self._stderr
        env = self._env
        if platform.system() != "Windows":
            self.subprocess = await asyncio.create_subprocess_exec(
                sys.executable,
                os.path.join(
                    os.path.dirname(os.path.realpath(__file__)), "chrome_wrapper.py"
                ),
                stdin=self.pipe.read_to_chromium,
                stdout=self.pipe.write_from_chromium,
                stderr=stderr,
                close_fds=True,
                env=env,
            )
        else:
            from .chrome_wrapper import open_browser
            self.subprocess = await open_browser(to_chromium=self.pipe.read_to_chromium,
                                                   from_chromium=self.pipe.write_from_chromium,
                                                   stderr=stderr,
                                                   env=env,
                                                   loop=True,
                                                   loop_hack=self.loop_hack)
        await self.populate_targets()
        self.future_self.set_result(self)

    # TODO create tempdir warning
    def _clean_temp(self):
        try:
            self.temp_dir.cleanup()
        except Exception as e:
            warnings.warn(str(e))

        # windows doesn't like python's default cleanup
        if platform.system() == "Windows":
            import stat
            import shutil

            def remove_readonly(func, path, excinfo):
                os.chmod(path, stat.S_IWUSR)
                func(path)

            try:
                shutil.rmtree(self.temp_dir.name, onexc=remove_readonly)
                del self.temp_dir
            except FileNotFoundError:
                pass # it worked!
            except PermissionError:
                warnings.warn(
                    "The temporary directory could not be deleted, due to permission error, execution will continue."
                )
            except Exception as e:
                warnings.warn(
                        f"The temporary directory could not be deleted, execution will continue. {type(e)}: {e}"
                )

    async def _is_closed_async(self, wait=0):
        waiter = self.subprocess.wait()
        try:
            await asyncio.wait_for(waiter, wait)
            return True
        except: # noqa
            return False

    def _is_closed(self, wait=0):
        if not wait:
            if not self.subprocess.poll():
                return False
            else:
                return True
        else:
            try:
                self.subprocess.wait(wait)
                return True
            except: # noqa
                return False

    # _sync_close and _async_close are basically the same thing

    def _sync_close(self):
        if self._is_closed():
            if self.debug: print("Browser was already closed.", file=sys.stderr)
            return
        # check if no sessions or targets
        self.send_command("Browser.close")
        if self._is_closed():
            if self.debug: print("Browser.close method closed browser", file=sys.stderr)
            return
        self.pipe.close()
        if self._is_closed(wait = 1):
            if self.debug: print("pipe.close() (or slow Browser.close) method closed browser", file=sys.stderr)
            return

        # Start a kill
        if platform.system() == "Windows":
            if not self._is_closed():
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(self.subprocess.pid)],
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                if self._is_closed(wait = 2):
                    return
                else:
                    raise RuntimeError("Couldn't kill browser subprocess")
        else:
            self.subprocess.terminate()
            if self._is_closed():
                if self.debug: print("terminate() closed the browser", file=sys.stderr)
                return

            self.subprocess.kill()
            if self._is_closed():
                if self.debug: print("kill() closed the browser", file=sys.stderr)
        return


    async def _async_close(self):
        if await self._is_closed_async():
            if self.debug: print("Browser was already closed.", file=sys.stderr)
            return
        # TODO: Above doesn't work with closed tabs for some reason
        # TODO: check if tabs?
        # TODO: track tabs?
        await asyncio.wait([self.send_command("Browser.close")], timeout=1)
        if await self._is_closed_async():
            if self.debug: print("Browser.close method closed browser", file=sys.stderr)
            return
        self.pipe.close()
        if await self._is_closed_async(wait=1):
            if self.debug: print("pipe.close() method closed browser", file=sys.stderr)
            return

        # Start a kill
        if platform.system() == "Windows":
            if not await self._is_closed_async():
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(self.subprocess.pid)],
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                if await self._is_closed_async(wait = 2):
                    return
                else:
                    raise RuntimeError("Couldn't kill browser subprocess")
        else:
            self.subprocess.terminate()
            if await self._is_closed_async():
                if self.debug: print("terminate() closed the browser", file=sys.stderr)
                return

            self.subprocess.kill()
            if await self._is_closed_async():
                if self.debug: print("kill() closed the browser", file=sys.stderr)
        return


    def close(self):
        if self.loop:
            async def close_task():
                await self._async_close()
                self.pipe.close()
                self._clean_temp() # can we make async
            return asyncio.create_task(close_task())
        else:
            self._sync_close()
            self.pipe.close()
            self._clean_temp()
        if self.debug:
            print(f"Tempfile still exists?: {bool(os.path.isfile(str(self.temp_dir.name)))}")


    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    async def __aexit__(self, type, value, traceback):
        await self.close()

class Browser(BrowserProcess, Target):
    def __init__(
        self,
        path=None,
        headless=True,
        debug=False,
        **kwargs
    ):
        self.tabs = OrderedDict()

        self.protocol = Protocol(self, debug=debug)

        # Initializing
        super(Target, self).__init__("0", self)
        self.add_session(Session(self, ""))

        super(BrowserProcess, self).__init__(path=None, headless=True, debug=False, **kwargs)


    # Basic syncronous functions

    def add_tab(self, tab):
        if not isinstance(tab, Tab):
            raise TypeError("tab must be an object of class Tab")
        self.tabs[tab.target_id] = tab

    def remove_tab(self, target_id):
        if isinstance(target_id, Tab):
            target_id = target_id.target_id
        del self.tabs[target_id]

    def get_tab(self):
        if self.tabs.values():
            return list(self.tabs.values())[0]

    # Better functions that require asyncronous
    async def create_tab(self, url="", width=None, height=None):
        if not self.loop:
            raise RuntimeError(
                "There is no eventloop, or was not passed to browser. Cannot use async methods"
            )
        if self.headless and (width or height):
            warnings.warn(
                "Width and height only work for headless chrome mode, they will be ignored."
            )
            width = None
            height = None
        params = dict(url=url)
        if width:
            params["width"] = width
        if height:
            params["height"] = height

        response = await self.browser.send_command("Target.createTarget", params=params)

        if "error" in response:
            raise RuntimeError("Could not create tab") from Exception(response["error"])
        target_id = response["result"]["targetId"]

        new_tab = Tab(target_id, self)
        self.add_tab(new_tab)

        await new_tab.create_session()

        return new_tab

    async def close_tab(self, target_id):
        if not self.loop:
            raise RuntimeError(
                "There is no eventloop, or was not passed to browser. Cannot use async methods"
            )
        if isinstance(target_id, Target):
            target_id = target_id.target_id
        # NOTE: we don't need to manually remove sessions because
        # sessions are intrinisically handled by events
        response = await self.send_command(
            command="Target.closeTarget",
            params={"targetId": target_id},
        )
        self.remove_tab(target_id)
        if "error" in response:
            raise RuntimeError("Could not close tab") from Exception(response["error"])
        return response

    async def create_session(self):
        if not self.browser.loop:
            raise RuntimeError(
                "There is no eventloop, or was not passed to browser. Cannot use async methods"
            )
        warnings.warn(
            "Creating new sessions on Browser() only works with some versions of Chrome, it is experimental."
        )
        response = await self.browser.send_command("Target.attachToBrowserTarget")
        if "error" in response:
            raise RuntimeError("Could not create session") from Exception(
                response["error"]
            )
        session_id = response["result"]["sessionId"]
        new_session = Session(self, session_id)
        self.add_session(new_session)
        return new_session

    async def populate_targets(self):
        if not self.browser.loop:
            warnings.warn("This method requires use of an event loop (asyncio).")
        response = await self.browser.send_command("Target.getTargets")
        if "error" in response:
            raise RuntimeError("Could not get targets") from Exception(
                response["error"]
            )

        for json_response in response["result"]["targetInfos"]:
            if (
                json_response["type"] == "page"
                and json_response["targetId"] not in self.tabs
            ):
                target_id = json_response["targetId"]
                new_tab = Tab(target_id, self)
                await new_tab.create_session()
                self.add_tab(new_tab)
                if self.debug:
                    print(f"The target {target_id} was added", file=sys.stderr)

    # Output Helper for Debugging

    def run_output_thread(self, debug=None):
        if self.loop:
            raise ValueError("You must use this method without loop in the Browser")
        if not debug:
            debug = self.debug

        def run_print(debug):
            if debug: print("Starting run_print loop", file=sys.stderr)
            while True:
                try:
                    responses = self.pipe.read_jsons(debug=debug)
                    for response in responses:
                        print(json.dumps(response, indent=4))
                except PipeClosedError:
                    if self.debug:
                        print("PipeClosedError caught", file=sys.stderr)
                    break

        Thread(target=run_print, args=(debug,)).start()
