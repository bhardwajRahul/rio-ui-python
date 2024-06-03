from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import inspect
import io
import json
import logging
import mimetypes
import secrets
import time
import traceback
import weakref
from datetime import timedelta
from pathlib import Path
from typing import *  # type: ignore
from xml.etree import ElementTree as ET

import crawlerdetect
import fastapi
import timer_dict
from PIL import Image
from uniserde import Jsonable, JsonDoc

import rio

from .. import (
    app,
    assets,
    byte_serving,
    components,
    data_models,
    errors,
    inspection,
    routing,
    utils,
)
from ..errors import AssetError
from ..serialization import serialize_json
from ..utils import URL
from .abstract_app_server import AbstractAppServer

try:
    import plotly  # type: ignore (missing import)
except ImportError:
    plotly = None

__all__ = [
    "FastapiServer",
]


P = ParamSpec("P")


# Used to identify search engine crawlers (like googlebot) and serve them
# without needing a websocket connection
CRAWLER_DETECTOR = crawlerdetect.CrawlerDetect()


@functools.lru_cache(maxsize=None)
def _build_sitemap(base_url: rio.URL, app: rio.App) -> str:
    # Find all pages to add
    page_urls = {
        rio.URL(""),
    }

    def worker(
        parent_url: rio.URL,
        page: rio.Page,
    ) -> None:
        cur_url = parent_url / page.page_url
        page_urls.add(cur_url)

        for child in page.children:
            worker(cur_url, child)

    for page in app.pages:
        worker(rio.URL(), page)

    # Build a XML site map
    tree = ET.Element(
        "urlset",
        xmlns="http://www.sitemaps.org/schemas/sitemap/0.9",
    )

    for relative_url in page_urls:
        full_url = base_url.with_path(relative_url.path)

        url = ET.SubElement(tree, "url")
        loc = ET.SubElement(url, "loc")
        loc.text = str(full_url)

    # Done
    return ET.tostring(tree, encoding="unicode", xml_declaration=True)


@functools.lru_cache(maxsize=None)
def read_frontend_template(template_name: str) -> str:
    """
    Read a text file from the frontend directory and return its content. The
    results are cached to avoid repeated disk access.
    """
    return (utils.FRONTEND_FILES_DIR / template_name).read_text(
        encoding="utf-8"
    )


def add_cache_headers(
    func: Callable[P, Awaitable[fastapi.Response]],
) -> Callable[P, Coroutine[None, None, fastapi.Response]]:
    """
    Decorator for routes that serve static files. Ensures that the response has
    the `Cache-Control` header set appropriately.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> fastapi.Response:
        response = await func(*args, **kwargs)
        response.headers["Cache-Control"] = "max-age=31536000, immutable"
        return response

    return wrapper


async def _periodically_clean_up_expired_sessions(
    app_server_ref: weakref.ReferenceType[FastapiServer],
):
    LOOP_INTERVAL = 60 * 15
    SESSION_LIFETIME = 60 * 60

    while True:
        await asyncio.sleep(LOOP_INTERVAL)

        app_server = app_server_ref()
        if app_server is None:
            return

        now = time.monotonic()
        cutoff = now - SESSION_LIFETIME

        for sess in app_server._active_session_tokens.values():
            if sess._last_interaction_timestamp < cutoff:
                sess.close()


class FastapiServer(fastapi.FastAPI, AbstractAppServer):
    def __init__(
        self,
        app_: app.App,
        debug_mode: bool,
        running_in_window: bool,
        internal_on_app_start: Callable[[], None] | None,
    ):
        super().__init__(
            title=app_.name,
            # summary=...,
            # description=...,
            openapi_url=None,
            # openapi_url="/openapi.json" if debug_mode else None,
            # docs_url="/docs" if debug_mode else None,
            # redoc_url="/redoc" if debug_mode else None,
            lifespan=__class__._lifespan,
        )

        self.app = app_
        self.debug_mode = debug_mode
        self.running_in_window = running_in_window
        self.internal_on_app_start = internal_on_app_start

        # While this Event is unset, no new Sessions can be created. This is
        # used to ensure that no clients (re-)connect while `rio run` is
        # reloading the app.
        self._can_create_new_sessions = asyncio.Event()
        self._can_create_new_sessions.set()

        # Initialized lazily, when the favicon is first requested.
        self._icon_as_png_blob: bytes | None = None

        # The session tokens and Request object for all clients that have made a
        # HTTP request, but haven't yet established a websocket connection. Once
        # the websocket connection is created, these will be turned into
        # Sessions.
        self._latent_session_tokens: dict[str, fastapi.Request] = {}

        # The session tokens for all active sessions. These allow clients to
        # identify themselves, for example to reconnect in case of a lost
        # connection.
        self._active_session_tokens: dict[str, rio.Session] = {}

        # All assets that have been registered with this session. They are held
        # weakly, meaning the session will host assets for as long as their
        # corresponding Python objects are alive.
        #
        # Assets registered here are hosted under `/asset/temp-{asset_id}`. In
        # addition the server also permanently hosts other "well known" assets
        # (such as javascript dependencies) which are available under public
        # URLS at `/asset/{some-name}`.
        self._assets: weakref.WeakValueDictionary[str, assets.Asset] = (
            weakref.WeakValueDictionary()
        )

        # All pending file uploads. These are stored in memory for a limited
        # time. When a file is uploaded the corresponding future is set.
        self._pending_file_uploads: timer_dict.TimerDict[
            str, asyncio.Future[list[utils.FileInfo]]
        ] = timer_dict.TimerDict(default_duration=timedelta(minutes=15))

        # FastAPI
        self.add_api_route("/robots.txt", self._serve_robots, methods=["GET"])
        self.add_api_route("/rio/sitemap", self._serve_sitemap, methods=["GET"])
        self.add_api_route(
            "/rio/favicon.png", self._serve_favicon, methods=["GET"]
        )
        self.add_api_route(
            "/rio/frontend/assets/{asset_id:path}",
            self._serve_frontend_asset,
        )
        self.add_api_route(
            "/rio/asset/special/{asset_id:path}",
            self._serve_special_asset,
            methods=["GET"],
        )
        self.add_api_route(
            "/rio/asset/hosted/{asset_id:path}",
            self._serve_hosted_asset,
            methods=["GET"],
        )
        self.add_api_route(
            "/rio/asset/user/{asset_id:path}",
            self._serve_user_asset,
            methods=["GET"],
        )
        self.add_api_route(
            "/rio/asset/temp/{asset_id:path}",
            self._serve_temp_asset,
            methods=["GET"],
        )
        self.add_api_route(
            "/rio/icon/{icon_name:path}", self._serve_icon, methods=["GET"]
        )
        self.add_api_route(
            "/rio/upload/{upload_token}",
            self._serve_file_upload,
            methods=["PUT"],
        )
        self.add_api_websocket_route("/rio/ws", self._serve_websocket)

        # Because this is a single page application, all other routes should
        # serve the index page. The session will determine which components
        # should be shown.
        self.add_api_route(
            "/{initial_route_str:path}", self._serve_index, methods=["GET"]
        )

    async def _call_on_app_start(self) -> None:
        rio._logger.debug("Calling `on_app_start`")

        if self.app._on_app_start is None:
            return

        try:
            result = self.app._on_app_start(self.app)

            if inspect.isawaitable(result):
                await result

        # Display and discard exceptions
        except Exception:
            print("Exception in `on_app_start` event handler:")
            traceback.print_exc()

    async def _call_on_app_close(self) -> None:
        rio._logger.debug("Calling `on_app_close`")

        if self.app._on_app_close is None:
            return

        try:
            result = self.app._on_app_close(self.app)

            if inspect.isawaitable(result):
                await result

        # Display and discard exceptions
        except Exception:
            print("Exception in `_on_app_close` event handler:")
            traceback.print_exc()

    @contextlib.asynccontextmanager
    async def _lifespan(self):
        # If running as a server, periodically clean up expired sessions
        if not self.running_in_window:
            asyncio.create_task(
                _periodically_clean_up_expired_sessions(weakref.ref(self)),
                name="Periodic session cleanup",
            )

        # Trigger the app's startup event
        #
        # This will be done blockingly, so the user can prepare any state before
        # connections are accepted. This is also why it's called before the
        # internal event.
        await self._call_on_app_start()

        # Trigger any internal startup event
        if self.internal_on_app_start is not None:
            self.internal_on_app_start()

        try:
            yield
        finally:
            # Close all sessions
            rio._logger.debug(
                f"App server shutting down; closing"
                f" {len(self._active_session_tokens)} active session(s)"
            )

            results = await asyncio.gather(
                *(
                    sess._close(close_remote_session=True)
                    for sess in self._active_session_tokens.values()
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    traceback.print_exception(
                        type(result), result, result.__traceback__
                    )

            await self._call_on_app_close()

    def url_for_user_asset(self, relative_asset_path: Path) -> rio.URL:
        return rio.URL(f"/rio/assets/user/{relative_asset_path}")

    def weakly_host_asset(self, asset: assets.HostedAsset) -> str:
        """
        Register an asset with this server. The asset will be held weakly,
        meaning the server will host assets for as long as their corresponding
        Python objects are alive.

        If another asset with the same id is already hosted, it will be
        replaced.
        """
        self._assets[asset.secret_id] = asset
        return f"/rio/assets/temp/{asset.secret_id}"

    def _get_all_meta_tags(self) -> list[str]:
        """
        Returns all `<meta>` tags that should be added to the app's HTML page.
        This includes auto-generated ones, as well as those stored directly in
        the app.
        """
        # Prepare the default tags
        all_tags = {
            "og:title": self.app.name,
            "description": self.app.description,
            "og:description": self.app.description,
            "keywords": "python, web, app, rio",
            "image": "/rio/favicon.png",
            "viewport": "width=device-width, initial-scale=1",
        }

        # Add the user-defined meta tags, overwriting any automatically
        # generated ones
        all_tags.update(self.app._custom_meta_tags)

        # Convert everything to HTML
        result: list[str] = []

        for key, value in all_tags.items():
            key = html.escape(key)
            value = html.escape(value)
            result.append(f'<meta name="{key}" content="{value}">')

        return result

    async def _serve_index(
        self,
        request: fastapi.Request,
        initial_route_str: str,
    ) -> fastapi.responses.Response:
        """
        Handler for serving the index HTML page via fastapi.
        """

        # Because Rio apps are single-page, this route serves as the fallback.
        # In addition to legitimate requests for HTML pages, it will also catch
        # a bunch of invalid requests to other resources. To highlight this,
        # throw a 404 if HTML is not explicitly requested.
        #
        # Currently inactive, because this caused issues behind dumb proxies
        # that don't pass on the `accept` header.

        # if not request.headers.get("accept", "").startswith("text/html"):
        #     raise fastapi.HTTPException(
        #         status_code=fastapi.status.HTTP_404_NOT_FOUND,
        #     )

        initial_messages = []

        is_crawler = CRAWLER_DETECTOR.isCrawler(
            request.headers.get("User-Agent")
        )
        if is_crawler:
            # If it's a crawler, immediately create a Session for it. Instead of
            # a websocket connection, outgoing messages are simply appended to a
            # list which will be included in the HTML.
            async def send_message(msg: JsonDoc) -> None:
                initial_messages.append(msg)

            async def receive_message() -> JsonDoc:
                while True:
                    await asyncio.sleep(999999)

            assert request.client is not None, "How can this happen?!"

            requested_url = rio.URL(str(request.url))
            try:
                session = await self.create_session(
                    initial_message=None,
                    websocket=None,
                    send_message=send_message,
                    receive_message=receive_message,
                    url=requested_url,
                    client_ip=request.client.host,
                    client_port=request.client.port,
                    http_headers=request.headers,
                )
            except routing.NavigationFailed:
                raise fastapi.HTTPException(
                    status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Navigation to initial page `{request.url}` has failed.",
                ) from None

            session.close()

            # If a page guard caused a redirect, tell that to the crawler in a
            # language it understands
            if session.active_page_url != requested_url:
                return fastapi.responses.RedirectResponse(
                    str(session.active_page_url)
                )

            session_token = "<crawler>"
        else:
            # Create a session token that uniquely identifies this client
            session_token = secrets.token_urlsafe()
            self._latent_session_tokens[session_token] = request

        # Load the template
        html_ = read_frontend_template("index.html")

        html_ = html_.replace("{session_token}", session_token)

        html_ = html_.replace(
            "'{child_attribute_names}'",
            json.dumps(
                inspection.get_child_component_containing_attribute_names_for_builtin_components()
            ),
        )

        html_ = html_.replace(
            "'{ping_pong_interval}'",
            str(self.app._ping_pong_interval.total_seconds()),
        )

        html_ = html_.replace(
            "'{debug_mode}'",
            "true" if self.debug_mode else "false",
        )

        html_ = html_.replace(
            "'{running_in_window}'",
            "true" if self.running_in_window else "false",
        )

        html_ = html_.replace(
            "'{initial_messages}'", json.dumps(initial_messages)
        )

        # Since the title is user-defined, it might contain placeholders like
        # `{debug_mode}`. So it's important that user-defined content is
        # inserted last.
        html_ = html_.replace("{title}", html.escape(self.app.name))

        # The placeholder for the metadata uses unescaped `<` and `>` characters
        # to ensure that no user-defined content can accidentally contain this
        # placeholder.
        html_ = html_.replace(
            '<meta name="{meta}" />',
            "\n".join(self._get_all_meta_tags()),
        )

        # Respond
        return fastapi.responses.HTMLResponse(html_)

    async def _serve_robots(
        self, request: fastapi.Request
    ) -> fastapi.responses.Response:
        """
        Handler for serving the `robots.txt` file via fastapi.
        """

        # TODO: Disallow internal API routes? Icons, assets, etc?
        request_url = URL(str(request.url))
        content = f"""
User-agent: *
Allow: /

Sitemap: {request_url.with_path("/rio/sitemap")}
        """.strip()

        return fastapi.responses.Response(
            content=content,
            media_type="text/plain",
        )

    async def _serve_sitemap(
        self, request: fastapi.Request
    ) -> fastapi.responses.Response:
        """
        Handler for serving the `sitemap.xml` file via fastapi.
        """
        return fastapi.responses.Response(
            content=_build_sitemap(rio.URL(str(request.url)), self.app),
            media_type="application/xml",
        )

    async def _serve_favicon(self) -> fastapi.responses.Response:
        """
        Handler for serving the favicon via fastapi, if one is set.
        """
        # If an icon is set, make sure a cached version exists
        if self._icon_as_png_blob is None and self.app._icon is not None:
            try:
                icon_blob, _ = await self.app._icon.try_fetch_as_blob()

                input_buffer = io.BytesIO(icon_blob)
                output_buffer = io.BytesIO()

                with Image.open(input_buffer) as image:
                    image.save(output_buffer, format="png")

            except Exception as err:
                raise fastapi.HTTPException(
                    status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not fetch the app's icon.",
                ) from err

            self._icon_as_png_blob = output_buffer.getvalue()

        # No icon set or fetching failed
        if self._icon_as_png_blob is None:
            return fastapi.responses.Response(status_code=404)

        # There is an icon, respond
        return fastapi.responses.Response(
            content=self._icon_as_png_blob,
            media_type="image/png",
        )

    async def _serve_frontend_asset(
        self,
        request: fastapi.Request,
        asset_id: str,
    ) -> fastapi.responses.Response:
        response = await self._serve_file_from_directory(
            request,
            utils.FRONTEND_ASSETS_DIR,
            asset_id + ".gz",
        )
        response.headers["content-encoding"] = "gzip"

        media_type = mimetypes.guess_type(asset_id, strict=False)[0]
        if media_type:
            response.headers["content-type"] = media_type

        return response

    @add_cache_headers
    async def _serve_special_asset(
        self,
        request: fastapi.Request,
        asset_id: str,
    ) -> fastapi.responses.Response:
        # The python plotly library already includes a minified version of
        # plotly.js. Rather than shipping another one, just serve the one
        # included in the library.
        if asset_id == "plotly.min.js":
            if plotly is None:
                return fastapi.responses.Response(status_code=404)

            return fastapi.responses.Response(
                content=plotly.offline.get_plotlyjs(),
                media_type="text/javascript",
            )

        return fastapi.responses.Response(status_code=404)

    async def _serve_hosted_asset(
        self,
        request: fastapi.Request,
        asset_id: str,
    ) -> fastapi.responses.Response:
        return await self._serve_file_from_directory(
            request,
            utils.HOSTED_ASSETS_DIR,
            asset_id,
        )

    async def _serve_user_asset(
        self,
        request: fastapi.Request,
        asset_id: str,
    ) -> fastapi.responses.Response:
        return await self._serve_file_from_directory(
            request,
            self.app.assets_dir,
            asset_id,
        )

    @add_cache_headers
    async def _serve_temp_asset(
        self,
        request: fastapi.Request,
        asset_id: str,
    ) -> fastapi.responses.Response:
        # Get the asset's Python instance. The asset's id acts as a secret, so
        # no further authentication is required.
        try:
            asset = self._assets[asset_id]
        except KeyError:
            return fastapi.responses.Response(status_code=404)

        # Fetch the asset's content and respond
        if isinstance(asset, assets.BytesAsset):
            return fastapi.responses.Response(
                content=asset.data,
                media_type=asset.media_type,
            )
        elif isinstance(asset, assets.PathAsset):
            return byte_serving.range_requests_response(
                request,
                file_path=asset.path,
                media_type=asset.media_type,
            )
        else:
            assert False, f"Unable to serve asset of unknown type: {asset}"

    @add_cache_headers
    async def _serve_file_from_directory(
        self,
        request: fastapi.Request,
        directory: Path,
        asset_id: str,
    ) -> fastapi.responses.Response:
        # Construct the path to the target file
        asset_file_path = directory / asset_id

        # Make sure the path is inside the hosted assets directory
        asset_file_path = asset_file_path.absolute()
        if not asset_file_path.is_relative_to(directory):
            logging.warning(
                f'Client requested asset "{asset_id}" which is not located'
                f" inside the assets directory. Somebody might be trying to"
                f" break out of the assets directory!"
            )
            return fastapi.responses.Response(status_code=404)

        return byte_serving.range_requests_response(
            request,
            file_path=asset_file_path,
        )

    async def _serve_icon(self, icon_name: str) -> fastapi.responses.Response:
        """
        Allows the client to request an icon by name. This is not actually the
        mechanism used by the `Icon` component, but allows JavaScript to request
        icons.
        """
        # Get the icon's SVG
        registry = components.Icon._get_registry()

        try:
            svg_source = registry.get_icon_svg(icon_name)
        except AssetError:
            return fastapi.responses.Response(status_code=404)

        # Respond
        return fastapi.responses.Response(
            content=svg_source,
            media_type="image/svg+xml",
        )

    async def _serve_file_upload(
        self,
        upload_token: str,
        file_names: list[str],
        file_types: list[str],
        file_sizes: list[str],
        # If no files are uploaded `files` isn't present in the form data at
        # all. Using a default value ensures that those requests don't fail
        # because of "missing parameters".
        #
        # Lists are mutable, make sure not to modify this value!
        file_streams: list[fastapi.UploadFile] = [],
    ):
        # Try to find the future for this token
        try:
            future = self._pending_file_uploads.pop(upload_token)
        except KeyError:
            raise fastapi.HTTPException(
                status_code=fastapi.status.HTTP_400_BAD_REQUEST,
                detail="Invalid upload token.",
            )

        # Make sure the same number of values was received for each parameter
        n_names = len(file_names)
        n_types = len(file_types)
        n_sizes = len(file_sizes)
        n_streams = len(file_streams)

        if n_names != n_types or n_names != n_sizes or n_names != n_streams:
            raise fastapi.HTTPException(
                status_code=fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Inconsistent number of files between the different message parts.",
            )

        # Parse the file sizes
        parsed_file_sizes: list[int] = []
        for file_size in file_sizes:
            try:
                parsed = int(file_size)
            except ValueError:
                raise fastapi.HTTPException(
                    status_code=fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid file size.",
                )

            if parsed < 0:
                raise fastapi.HTTPException(
                    status_code=fastapi.status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid file size.",
                )

            parsed_file_sizes.append(parsed)

        # Complete the future
        future.set_result(
            [
                utils.FileInfo(
                    name=file_names[ii],
                    size_in_bytes=parsed_file_sizes[ii],
                    media_type=file_types[ii],
                    _contents=await file_streams[ii].read(),
                )
                for ii in range(n_names)
            ]
        )

        return fastapi.responses.Response(
            status_code=fastapi.status.HTTP_200_OK
        )

    async def file_chooser(
        self,
        session: rio.Session,
        *,
        file_extensions: Iterable[str] | None = None,
        multiple: bool = False,
    ) -> utils.FileInfo | tuple[utils.FileInfo, ...]:
        # Create a secret id and register the file upload with the app server
        upload_id = secrets.token_urlsafe()
        future = asyncio.Future[list[utils.FileInfo]]()

        self._pending_file_uploads[upload_id] = future

        # Allow the user to specify both `jpg` and `.jpg`
        if file_extensions is not None:
            file_extensions = [
                ext if ext.startswith(".") else f".{ext}"
                for ext in file_extensions
            ]

        # Tell the frontend to upload a file
        await session._request_file_upload(
            upload_url=f"/rio/upload/{upload_id}",
            file_extensions=file_extensions,
            multiple=multiple,
        )

        # Wait for the user to upload files
        files = await future

        # Raise an exception if no files were uploaded
        if not files:
            raise errors.NoFileSelectedError()

        # Ensure only one file was provided if `multiple` is False
        if not multiple and len(files) != 1:
            logging.warning(
                "Client attempted to upload multiple files when `multiple` was False."
            )
            raise errors.NoFileSelectedError()

        # Return the file info
        if multiple:
            return tuple(files)  # type: ignore
        else:
            return files[0]

    @contextlib.contextmanager
    def temporarily_disable_new_session_creation(self):
        self._can_create_new_sessions.clear()

        try:
            yield
        finally:
            self._can_create_new_sessions.set()

    async def _serve_websocket(
        self,
        websocket: fastapi.WebSocket,
        sessionToken: str,
    ):
        """
        Handler for establishing the websocket connection and handling any
        messages.
        """
        # Blah, naming conventions
        session_token = sessionToken
        del sessionToken

        # Wait until we're allowed to accept new websocket connections. (This is
        # temporarily disable while `rio run` is reloading the app.)
        await self._can_create_new_sessions.wait()

        # Accept the socket. I can't figure out how to access the close reason
        # in JS unless the connection was accepted first, so we have to do this
        # even if we are going to immediately close it again.
        await websocket.accept()

        # Look up the session token. If it is valid the session's duration is
        # refreshed so it doesn't expire. If the token is not valid, don't
        # accept the websocket.
        try:
            request = self._latent_session_tokens.pop(session_token)
        except KeyError:
            # Check if this is a reconnect
            try:
                sess = self._active_session_tokens[session_token]
            except KeyError:
                # Inform the client that this session token is invalid
                await websocket.close(
                    3000,  # Custom error code
                    "Invalid session token.",
                )
                return

            # The session is active again. Set the corresponding event
            sess._is_active_event.set()

            init_coro = sess._send_all_components_on_reconnect()
        else:
            sess = await self._create_session_from_websocket(
                session_token, request, websocket
            )
            self._active_session_tokens[session_token] = sess

            # Trigger a refresh. This will also send the initial state to
            # the frontend.
            init_coro = sess._refresh()

        try:
            # This is done in a task because `sess.serve()` is not yet running,
            # so if the method needs a response from the frontend, it would hang
            # indefinitely.
            asyncio.create_task(init_coro)

            # Serve the websocket
            await sess.serve()
        except asyncio.CancelledError:
            pass

        except fastapi.WebSocketDisconnect as err:
            # If the connection was closed on purpose, close the session
            if err.code == 1001:
                sess.close()

        else:
            # I don't think this branch can even be reached, but better safe
            # than sorry, I guess?
            sess.close()
        finally:
            sess._websocket = None
            sess._is_active_event.clear()

    async def _create_session_from_websocket(
        self,
        session_token: str,
        request: fastapi.Request,
        websocket: fastapi.WebSocket,
    ) -> rio.Session:
        assert request.client is not None, "Why can this happen?"

        async def send_message(msg: JsonDoc) -> None:
            text = serialize_json(msg)
            try:
                await websocket.send_text(text)
            except RuntimeError:  # Socket is already closed
                pass

        async def receive_message() -> JsonDoc:
            # Refresh the session's duration
            self._active_session_tokens[session_token] = sess

            # Fetch a message
            try:
                result = await websocket.receive_json()
            except RuntimeError:  # Socket is already closed
                # Not clear which error code to use
                raise fastapi.WebSocketDisconnect(-1)

            return result

        # Upon connecting, the client sends an initial message containing
        # information about it. Wait for that, but with a timeout - otherwise
        # evildoers could overload the server with connections that never send
        # anything.
        initial_message_json: Jsonable = await asyncio.wait_for(
            websocket.receive_json(),
            timeout=60,
        )
        initial_message = data_models.InitialClientMessage.from_json(
            initial_message_json  # type: ignore
        )

        try:
            sess = await self.create_session(
                initial_message,
                websocket=websocket,
                send_message=send_message,
                receive_message=receive_message,
                url=rio.URL(str(request.url)),
                client_ip=request.client.host,
                client_port=request.client.port,
                http_headers=request.headers,
            )
        except routing.NavigationFailed:
            # TODO: Notify the client? Show an error?
            raise fastapi.HTTPException(
                status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Navigation to initial page `{request.url}` has failed.",
            ) from None

        return sess
