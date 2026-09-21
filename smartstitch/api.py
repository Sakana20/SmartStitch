from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .audio_preview import AudioPreviewError, create_audio_preview
from .collaboration import (
    CollaborationError,
    ConfigLeaseError,
    ConfigLeaseManager,
    ConfigLockHeldError,
    ConfigVersionConflictError,
    UserProfileRequiredError,
    UserProfileStore,
)
from .config import ConfigError, ConfigStore
from .database import SQLiteStore
from .feishu import (
    FeishuBaseClient,
    FeishuError,
    FeishuSettingsStore,
    FeishuSyncManager,
)
from .jobs import JobManager, TERMINAL_STATES
from .library import (
    MAX_VISUAL_BORDER_BYTES,
    LibraryConflictError,
    LibraryError,
    LibraryService,
    pick_directory,
)
from .media import DisconnectSafeFileResponse
from .models import (
    AddBenefitRequest,
    AddPoolRequest,
    CloneConfigRequest,
    ConfigLockAcquireRequest,
    ConfigLockActionRequest,
    ConfigUpdateRequest,
    CreateConfigRequest,
    CreateLibraryRequest,
    DeletePoolRequest,
    FeishuConnectionTestRequest,
    FeishuSettingsUpdateRequest,
    GlobalVisualBorderUpdateRequest,
    JobCreateRequest,
    LibraryPreflightRequest,
    LoudnessPreviewRequest,
    PreviewRequest,
    ReplaceOverlayImageRequest,
    ReorderTimelineRequest,
    StructuredConfigUpdateRequest,
    TimelineAnalyzeRequest,
    TimelineDecisionRequest,
    TimelineSourceDirectoryRequest,
    TimelineSliceRequest,
    UpdatePoolRequest,
    UserProfileUpdateRequest,
    WeightUpdateRequest,
)
from .planner import PlanError, build_plan
from .probe_cache import MediaProbeCache
from .runtime import (
    configure_bundled_media_tools,
    is_frozen,
    resource_root,
    seed_packaged_configs,
    writable_config_directory,
    writable_data_directory,
)
from .scanner import probe_config_audio, scan_config
from .slice_jobs import SliceJobManager
from .slicer import SliceConflictError, SliceError, TimelineSlicer
from .timeline import TimelineAnalyzer, TimelineError, list_source_videos
from .updater import GitHubReleaseChecker, UpdateCheckError
from .visual_borders import (
    VisualBorderLibrary,
    VisualBorderLibraryConflict,
    VisualBorderLibraryError,
)


DEFAULT_SHARED_CONFIG_DIRECTORY = Path("/Volumes/home/Smartstitch")
DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT = Path("/Volumes/homes")
CONFIG_DIRECTORY_ENV = "SMARTSTITCH_CONFIG_DIRECTORY"


class SharedConfigUnavailableError(RuntimeError):
    pass


def alternate_shared_config_directories(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(
        (
            candidate
            for candidate in parent.glob("*/Smartstitch")
            if candidate.is_dir()
        ),
        key=lambda candidate: str(candidate).casefold(),
    )


def resolve_config_directory(
    application_root: Path,
    *,
    allow_shared_default: bool,
    shared_directory: Path = DEFAULT_SHARED_CONFIG_DIRECTORY,
    alternate_shared_parent: Path = DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT,
) -> Path:
    override = os.environ.get(CONFIG_DIRECTORY_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if allow_shared_default and shared_directory.is_dir():
        return shared_directory.resolve()
    if allow_shared_default:
        alternatives = alternate_shared_config_directories(
            alternate_shared_parent
        )
        if len(alternatives) == 1:
            return alternatives[0].resolve()
        raise SharedConfigUnavailableError(
            "未连接 NAS：找不到 /Volumes/home/Smartstitch。"
            "请先在 Finder 中连接 NAS 并确认共享目录已挂载，然后重新启动 SmartStitch。"
        )
    return application_root / "config"


def canonical_config_directory(directory: Path) -> Path:
    try:
        relative = directory.relative_to(DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT)
    except ValueError:
        return directory
    if len(relative.parts) == 2 and relative.parts[1] == "Smartstitch":
        return DEFAULT_SHARED_CONFIG_DIRECTORY
    return directory


def create_app(
    base_directory: Path | None = None,
    config_directory: Path | None = None,
    data_directory: Path | None = None,
) -> FastAPI:
    root = (base_directory or resource_root()).resolve()
    configure_bundled_media_tools(root)
    local_config_directory = writable_config_directory(root)
    if config_directory is not None:
        resolved_config_directory = config_directory.expanduser().resolve()
    else:
        try:
            resolved_config_directory = resolve_config_directory(
                root,
                allow_shared_default=base_directory is None,
            )
        except SharedConfigUnavailableError:
            if not is_frozen():
                raise
            resolved_config_directory = local_config_directory
    if resolved_config_directory == local_config_directory:
        seed_packaged_configs(root / "config", resolved_config_directory)
    resolved_data_directory = (
        data_directory.expanduser().resolve()
        if data_directory is not None
        else writable_data_directory(root)
    )
    config_store = ConfigStore(
        resolved_config_directory,
        canonical_directory=canonical_config_directory(
            resolved_config_directory
        ),
    )
    user_profiles = UserProfileStore(resolved_data_directory)
    config_leases = ConfigLeaseManager(config_store)
    library_service = LibraryService(config_store)
    visual_border_library = VisualBorderLibrary(config_store.directory)
    database_store = SQLiteStore(resolved_data_directory / "smartstitch.db")
    media_probe_cache = MediaProbeCache(database_store)
    try:
        media_probe_cache.prune()
    except Exception:
        pass
    job_manager = JobManager(
        config_store,
        resolved_data_directory,
        database_store,
        visual_border_library=visual_border_library,
        media_probe_cache=media_probe_cache,
    )
    feishu_settings = FeishuSettingsStore(resolved_data_directory)
    feishu_sync_manager = FeishuSyncManager(
        database_store, feishu_settings, job_manager.get_job
    )
    job_manager.output_sync_manager = feishu_sync_manager
    feishu_sync_manager.resume_interrupted()
    timeline_analyzer = TimelineAnalyzer(resolved_data_directory / "timelines")
    timeline_slicer = TimelineSlicer(timeline_analyzer, library_service)
    slice_job_manager = SliceJobManager(timeline_slicer, database_store)
    release_checker = GitHubReleaseChecker()
    app = FastAPI(title="SmartStitch", version=__version__)
    app.state.root = root
    app.state.data_directory = resolved_data_directory
    app.state.config_store = config_store
    app.state.user_profiles = user_profiles
    app.state.config_leases = config_leases
    app.state.library_service = library_service
    app.state.visual_border_library = visual_border_library
    app.state.job_manager = job_manager
    app.state.feishu_settings = feishu_settings
    app.state.feishu_sync_manager = feishu_sync_manager
    app.state.feishu_client_factory = FeishuBaseClient
    app.state.database_store = database_store
    app.state.media_probe_cache = media_probe_cache
    app.state.timeline_analyzer = timeline_analyzer
    app.state.timeline_slicer = timeline_slicer
    app.state.slice_job_manager = slice_job_manager
    app.state.release_checker = release_checker

    def collaboration_http_error(exc: CollaborationError) -> HTTPException:
        if isinstance(exc, ConfigLockHeldError):
            return HTTPException(
                423,
                {
                    "code": "config_locked",
                    "message": str(exc),
                    **exc.status,
                },
            )
        if isinstance(exc, ConfigVersionConflictError):
            return HTTPException(409, {"code": "config_changed", "message": str(exc)})
        if isinstance(exc, UserProfileRequiredError):
            return HTTPException(428, {"code": "user_required", "message": str(exc)})
        if isinstance(exc, ConfigLeaseError):
            return HTTPException(423, {"code": "lease_invalid", "message": str(exc)})
        return HTTPException(422, str(exc))

    def request_lease_token(request: Request) -> str:
        return request.headers.get("X-SmartStitch-Lease", "").strip()

    def request_config_hash(request: Request) -> str:
        return request.headers.get("X-SmartStitch-Config-Hash", "").strip()

    @app.get("/api/v1/system/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "version": __version__,
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "config_directory": str(config_store.directory),
            "canonical_config_directory": str(
                config_store.canonical_directory
            ),
            "shared_config": config_store.directory != local_config_directory,
            "config_path_mapped": config_store.has_path_alias,
        }

    @app.get("/api/v1/system/updates")
    def check_for_updates() -> dict[str, object]:
        try:
            return app.state.release_checker.check(__version__)
        except UpdateCheckError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/v1/system/updates/open")
    def open_latest_release() -> dict[str, object]:
        try:
            return app.state.release_checker.open_latest_release()
        except UpdateCheckError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/v1/system/directory-picker")
    def directory_picker() -> dict[str, object]:
        try:
            return pick_directory()
        except (LibraryError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/integrations/feishu/settings")
    def get_feishu_settings() -> dict[str, object]:
        try:
            return feishu_settings.public()
        except FeishuError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/integrations/feishu/settings")
    def update_feishu_settings(
        request: FeishuSettingsUpdateRequest,
    ) -> dict[str, object]:
        try:
            return feishu_settings.update(request.app_id, request.app_secret)
        except (FeishuError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/integrations/feishu/test")
    def test_feishu_connection(
        request: FeishuConnectionTestRequest,
    ) -> dict[str, object]:
        try:
            app_id, app_secret = feishu_settings.credentials(
                request.app_id, request.app_secret
            )
            client = app.state.feishu_client_factory(app_id, app_secret)
            base_token, linked_table_id = client.resolve_base_url(request.base_url)
            tables = client.list_tables(base_token)
            if linked_table_id and not any(
                table["table_id"] == linked_table_id for table in tables
            ):
                tables.append(client.get_table(base_token, linked_table_id))
            selected_table_id = linked_table_id
            if selected_table_id and not any(
                table["table_id"] == selected_table_id for table in tables
            ):
                selected_table_id = None
            if not selected_table_id and len(tables) == 1:
                selected_table_id = str(tables[0]["table_id"])
            return {
                "ok": True,
                "base_token": base_token,
                "linked_table_id": linked_table_id,
                "selected_table_id": selected_table_id,
                "tables": tables,
            }
        except FeishuError as exc:
            raise HTTPException(
                422,
                {
                    "code": exc.code,
                    "message": str(exc),
                    "required_scopes": exc.required_scopes,
                    "console_url": exc.console_url,
                },
            ) from exc

    @app.get("/api/v1/users/me")
    def get_current_user() -> dict[str, object]:
        try:
            return user_profiles.get()
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    @app.put("/api/v1/users/me")
    def update_current_user(request: UserProfileUpdateRequest) -> dict[str, object]:
        try:
            return user_profiles.update(
                request.display_name, switch_user=request.switch_user
            )
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    @app.post("/api/v1/libraries/preflight")
    def preflight_library(request: LibraryPreflightRequest) -> dict[str, object]:
        try:
            return library_service.preflight(request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/libraries")
    def create_library(request: CreateLibraryRequest) -> dict[str, object]:
        try:
            with config_leases.commit_guard(request.new_id):
                return library_service.create(request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/libraries/by-config/{config_id}")
    def get_library(config_id: str) -> dict[str, object]:
        try:
            # Legacy library inspection can perform a one-time layout migration.
            # Serialize that hidden write with normal configuration commits.
            with config_leases.commit_guard(config_id):
                return library_service.inspect_by_config(config_id)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/libraries/by-config/{config_id}/slice-targets")
    def get_slice_targets(config_id: str) -> dict[str, object]:
        try:
            return {"targets": library_service.slice_targets(config_id)}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except (LibraryError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/sources/{category}/open-directory")
    def open_source_directory(config_id: str, category: str) -> dict[str, object]:
        try:
            return library_service.open_source_directory(config_id, category)
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except (LibraryError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/benefits")
    def add_benefit(
        config_id: str, request: AddBenefitRequest, http_request: Request
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.add_benefit(config_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/pools")
    def add_pool(
        config_id: str, request: AddPoolRequest, http_request: Request
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.add_pool(config_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/v1/configs/{config_id}/pools/{pool_id}")
    def update_pool(
        config_id: str,
        pool_id: str,
        request: UpdatePoolRequest,
        http_request: Request,
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.update_pool(config_id, pool_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/v1/configs/{config_id}/pools/{pool_id}")
    def delete_pool(
        config_id: str,
        pool_id: str,
        request: DeletePoolRequest,
        http_request: Request,
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.delete_pool(config_id, pool_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}/timeline")
    def reorder_timeline(
        config_id: str, request: ReorderTimelineRequest, http_request: Request
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.reorder_timeline(config_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/overlay-image")
    def replace_overlay_image(
        config_id: str,
        request: ReplaceOverlayImageRequest,
        http_request: Request,
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id, request_lease_token(http_request), request.current_config_hash
            ):
                return library_service.replace_overlay_image(config_id, request)
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/visual-border")
    async def replace_visual_border(
        config_id: str,
        http_request: Request,
    ) -> dict[str, object]:
        encoded_filename = http_request.headers.get(
            "X-SmartStitch-Filename", ""
        ).strip()
        filename = urllib.parse.unquote(encoded_filename)
        if not filename:
            raise HTTPException(422, "缺少边框文件名")
        content_length = http_request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_VISUAL_BORDER_BYTES:
                    raise HTTPException(413, "视觉去重边框不能超过 500 MB")
            except ValueError as exc:
                raise HTTPException(422, "上传文件大小无效") from exc

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="smartstitch-visual-border-",
            suffix=Path(filename).suffix.lower(),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        total = 0
        try:
            with temporary_path.open("wb") as handle:
                async for chunk in http_request.stream():
                    total += len(chunk)
                    if total > MAX_VISUAL_BORDER_BYTES:
                        raise HTTPException(413, "视觉去重边框不能超过 500 MB")
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(422, "视觉去重边框不能为空")
            try:
                with config_leases.write_guard(
                    config_id,
                    request_lease_token(http_request),
                    request_config_hash(http_request),
                ):
                    return library_service.replace_visual_border(
                        config_id,
                        filename=filename,
                        uploaded_path=temporary_path,
                        current_config_hash=request_config_hash(http_request),
                    )
            except CollaborationError as exc:
                raise collaboration_http_error(exc) from exc
            except LibraryConflictError as exc:
                raise HTTPException(409, str(exc)) from exc
            except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc
        finally:
            temporary_path.unlink(missing_ok=True)

    @app.get("/api/v1/global-assets/visual-borders")
    def list_global_visual_borders() -> dict[str, object]:
        try:
            return visual_border_library.list()
        except VisualBorderLibraryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/global-assets/visual-borders")
    async def upload_global_visual_border(
        http_request: Request,
    ) -> dict[str, object]:
        encoded_filename = http_request.headers.get(
            "X-SmartStitch-Filename", ""
        ).strip()
        filename = urllib.parse.unquote(encoded_filename)
        if not filename:
            raise HTTPException(422, "缺少边框文件名")
        revision_header = http_request.headers.get(
            "X-SmartStitch-Library-Revision", ""
        ).strip()
        try:
            expected_revision = int(revision_header)
        except ValueError as exc:
            raise HTTPException(422, "缺少或无效的全局边框库版本") from exc
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="smartstitch-global-visual-border-",
            suffix=Path(filename).suffix.lower(),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        total = 0
        try:
            with temporary_path.open("wb") as handle:
                async for chunk in http_request.stream():
                    total += len(chunk)
                    if total > MAX_VISUAL_BORDER_BYTES:
                        raise HTTPException(413, "视觉去重边框不能超过 500 MB")
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(422, "视觉去重边框不能为空")
            return visual_border_library.upload(
                filename, temporary_path, expected_revision
            )
        except VisualBorderLibraryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (VisualBorderLibraryError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            temporary_path.unlink(missing_ok=True)

    @app.patch("/api/v1/global-assets/visual-borders/{asset_id}")
    def update_global_visual_border(
        asset_id: str,
        request: GlobalVisualBorderUpdateRequest,
    ) -> dict[str, object]:
        try:
            return visual_border_library.update(
                asset_id,
                expected_revision=request.library_revision,
                updates=request.model_dump(exclude={"library_revision"}),
            )
        except VisualBorderLibraryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except VisualBorderLibraryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/v1/global-assets/visual-borders/{asset_id}")
    def disable_global_visual_border(
        asset_id: str,
        http_request: Request,
    ) -> dict[str, object]:
        revision_header = http_request.headers.get(
            "X-SmartStitch-Library-Revision", ""
        ).strip()
        try:
            expected_revision = int(revision_header)
        except ValueError as exc:
            raise HTTPException(422, "缺少或无效的全局边框库版本") from exc
        try:
            return visual_border_library.update(
                asset_id,
                expected_revision=expected_revision,
                updates={"enabled": False},
            )
        except VisualBorderLibraryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except VisualBorderLibraryError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/configs")
    def list_configs() -> list[dict[str, object]]:
        return config_store.list()

    @app.get("/api/v1/configs/{config_id}/lock")
    def get_config_lock(config_id: str) -> dict[str, object]:
        try:
            config_store.path_for(config_id).stat()
            return config_leases.status(config_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    def acquire_config_lock(
        config_id: str, request: ConfigLockAcquireRequest, *, takeover: bool
    ) -> dict[str, object]:
        try:
            profile, device = user_profiles.require()
            result = config_leases.acquire(
                config_id,
                profile,
                device,
                request.browser_session_id,
                takeover=takeover,
            )
            loaded = config_store.load(config_id)
            return {
                **result,
                "config": loaded.model_dump(mode="json"),
                "yaml_text": config_store.raw(config_id),
                "content_hash": config_store.content_hash(config_id),
            }
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    @app.post("/api/v1/configs/{config_id}/lock/acquire")
    def acquire_config_lock_route(
        config_id: str, request: ConfigLockAcquireRequest
    ) -> dict[str, object]:
        return acquire_config_lock(config_id, request, takeover=False)

    @app.post("/api/v1/configs/{config_id}/lock/takeover")
    def takeover_config_lock_route(
        config_id: str, request: ConfigLockAcquireRequest
    ) -> dict[str, object]:
        return acquire_config_lock(config_id, request, takeover=True)

    @app.post("/api/v1/configs/{config_id}/lock/renew")
    def renew_config_lock(
        config_id: str, request: ConfigLockActionRequest
    ) -> dict[str, object]:
        try:
            return config_leases.renew(
                config_id, request.lease_token, request.browser_session_id
            )
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    @app.post("/api/v1/configs/{config_id}/lock/release")
    def release_config_lock(
        config_id: str, request: ConfigLockActionRequest
    ) -> dict[str, object]:
        try:
            return config_leases.release(
                config_id, request.lease_token, request.browser_session_id
            )
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc

    @app.post("/api/v1/configs")
    def create_config(request: CreateConfigRequest) -> dict[str, object]:
        try:
            with config_leases.commit_guard(request.new_id):
                config = config_store.create(request.new_id, request.new_name.strip())
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config.id),
            }
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/configs/{config_id}")
    def get_config(config_id: str) -> dict[str, object]:
        try:
            config = config_store.load(config_id)
            return {
                "config": config.model_dump(mode="json"),
                "yaml_text": config_store.raw(config_id),
                "content_hash": config_store.content_hash(config_id),
            }
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}")
    def update_config(
        config_id: str, request: ConfigUpdateRequest, http_request: Request
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id,
                request_lease_token(http_request),
                request_config_hash(http_request),
            ):
                config = config_store.save_text(config_id, request)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config_id),
            }
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}/structured")
    def update_structured_config(
        config_id: str,
        request: StructuredConfigUpdateRequest,
        http_request: Request,
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id,
                request_lease_token(http_request),
                request_config_hash(http_request),
            ):
                config = config_store.save_config(config_id, request.config)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config_id),
            }
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/clone")
    def clone_config(config_id: str, request: CloneConfigRequest) -> dict[str, object]:
        try:
            with config_leases.commit_guard(request.new_id):
                config = config_store.clone(config_id, request.new_id, request.new_name)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config.id),
            }
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/v1/configs/{config_id}")
    def delete_config(config_id: str, http_request: Request) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id,
                request_lease_token(http_request),
                request_config_hash(http_request),
            ):
                backup = config_store.delete(config_id)
            return {"ok": True, "backup": str(backup)}
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/weights")
    def update_weights(
        config_id: str, request: WeightUpdateRequest, http_request: Request
    ) -> dict[str, object]:
        try:
            with config_leases.write_guard(
                config_id,
                request_lease_token(http_request),
                request_config_hash(http_request),
            ):
                config = config_store.update_weights(config_id, request.items)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config_id),
            }
        except CollaborationError as exc:
            raise collaboration_http_error(exc) from exc
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/scan")
    def scan(config_id: str) -> dict[str, object]:
        try:
            config = config_store.load(config_id)
            result = scan_config(
                config,
                visual_border_library.assets_for_config(config),
                media_probe_cache,
            )
            return {**result.model_dump(mode="json"), "ok": result.ok}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/plans/preview")
    def preview(request: PreviewRequest) -> dict[str, object]:
        try:
            config = config_store.load(request.config_id)
            if request.output_directory:
                config.output.directory = request.output_directory
            result = build_plan(
                config,
                scan_config(
                    config,
                    visual_border_library.assets_for_config(config),
                    media_probe_cache,
                ),
                request.count,
                request.seed,
            )
            return result.model_dump(mode="json")
        except (ConfigError, FileNotFoundError, PlanError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def audio_preview_response(request: LoudnessPreviewRequest) -> FileResponse:
        try:
            config = config_store.load(request.config_id)
            requested_path = Path(request.asset_path).expanduser().resolve()
            probe_config_audio(config, requested_path)
            preview_path = create_audio_preview(
                request, requested_path, resolved_data_directory / "previews"
            )
            return FileResponse(
                preview_path,
                media_type="audio/mp4",
                headers={"Cache-Control": "private, max-age=3600"},
            )
        except (ConfigError, FileNotFoundError, ValueError, AudioPreviewError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/audio/preview")
    def audio_preview(request: LoudnessPreviewRequest) -> FileResponse:
        return audio_preview_response(request)

    @app.get("/api/v1/audio/preview")
    def audio_preview_for_player(
        config_id: str,
        asset_path: str,
        normalized: bool = True,
        target_lufs: float = -14,
        loudness_range_lu: float = 7,
        true_peak_dbtp: float = -1.5,
        duration_seconds: float = 12,
    ) -> FileResponse:
        try:
            request = LoudnessPreviewRequest(
                config_id=config_id,
                asset_path=asset_path,
                normalized=normalized,
                target_lufs=target_lufs,
                loudness_range_lu=loudness_range_lu,
                true_peak_dbtp=true_peak_dbtp,
                duration_seconds=duration_seconds,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return audio_preview_response(request)

    @app.post("/api/v1/timeline/analyze")
    def analyze_timeline(request: TimelineAnalyzeRequest) -> dict[str, object]:
        try:
            return timeline_analyzer.analyze(
                request.source_path,
                request.scene_threshold,
                request.silence_duration_seconds,
            )
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/sources")
    def timeline_sources(
        request: TimelineSourceDirectoryRequest,
    ) -> dict[str, object]:
        try:
            return list_source_videos(request.source_directory)
        except (TimelineError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/timeline/media/{media_token}")
    def timeline_media(media_token: str) -> DisconnectSafeFileResponse:
        try:
            return DisconnectSafeFileResponse(timeline_analyzer.media_path(media_token))
        except TimelineError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/timeline/waveforms/{analysis_id}")
    def timeline_waveform(
        analysis_id: str,
        start_frame: int = Query(ge=0),
        end_frame: int = Query(gt=0),
        width_px: int = Query(ge=1, le=4096),
    ) -> dict[str, object]:
        try:
            return timeline_analyzer.waveform(
                analysis_id, start_frame, end_frame, width_px
            )
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/timeline/decisions")
    def save_timeline_decision(request: TimelineDecisionRequest) -> dict[str, object]:
        try:
            return timeline_analyzer.save_decision(request.analysis_id, request.frame_indexes)
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/slices")
    def export_timeline_slices(request: TimelineSliceRequest) -> dict[str, object]:
        try:
            return timeline_slicer.export(request)
        except SliceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (
            SliceError,
            TimelineError,
            LibraryError,
            ConfigError,
            ValueError,
            OSError,
        ) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/slice-jobs", status_code=202)
    def create_timeline_slice_job(
        request: TimelineSliceRequest,
    ) -> dict[str, object]:
        try:
            return slice_job_manager.create(request)
        except SliceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (
            SliceError,
            TimelineError,
            LibraryError,
            ConfigError,
            ValueError,
            OSError,
        ) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/timeline/slice-jobs")
    def list_timeline_slice_jobs(
        limit: int = Query(default=20, ge=1, le=200),
        status: str | None = None,
    ) -> list[dict[str, object]]:
        return slice_job_manager.list_jobs(status=status)[:limit]

    @app.get("/api/v1/timeline/slice-jobs/events")
    async def timeline_slice_job_events() -> StreamingResponse:
        async def stream():
            previous = ""
            while True:
                jobs = slice_job_manager.list_jobs()[:20]
                payload = json.dumps(jobs, ensure_ascii=False)
                if payload != previous:
                    yield f"event: slice_jobs_update\ndata: {payload}\n\n"
                    previous = payload
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/timeline/slice-jobs/{job_id}")
    def get_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/cancel")
    def cancel_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/retry-failed")
    def retry_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.retry(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except (ValueError, LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/resume")
    def resume_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.retry(job_id, interrupted_only=True)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except (ValueError, LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/v1/timeline/slice-jobs/{job_id}")
    def delete_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            slice_job_manager.delete(job_id)
            return {"ok": True, "job_id": job_id}
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/timeline/slice-jobs/{job_id}/manifest")
    def timeline_slice_job_manifest(job_id: str) -> FileResponse:
        try:
            job = slice_job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        path = Path(str(job["manifest_path"]))
        if not path.is_file():
            raise HTTPException(404, "切片清单不存在")
        return FileResponse(path)

    @app.post("/api/v1/jobs")
    def create_job(request: JobCreateRequest) -> dict[str, object]:
        try:
            return job_manager.create(request)
        except (ConfigError, FileNotFoundError, PlanError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return job_manager.list_jobs()

    @app.delete("/api/v1/jobs")
    def delete_all_jobs() -> dict[str, object]:
        try:
            return {"ok": True, "deleted_count": job_manager.delete_all()}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.post("/api/v1/jobs/{job_id}/start")
    def start_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.start(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.delete("/api/v1/jobs/{job_id}")
    def delete_job(job_id: str) -> dict[str, object]:
        try:
            job_manager.delete(job_id)
            return {"ok": True, "job_id": job_id}
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/manifest")
    def manifest(job_id: str) -> FileResponse:
        try:
            job = job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return FileResponse(Path(job["output_directory"]) / "manifest.json")

    @app.get("/api/v1/jobs/{job_id}/sync/feishu")
    def get_job_feishu_sync(job_id: str) -> dict[str, object]:
        try:
            job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        record = feishu_sync_manager.get(job_id)
        return record or {
            "job_id": job_id,
            "provider": "feishu_base",
            "status": "not_started",
        }

    @app.post("/api/v1/jobs/{job_id}/sync/feishu", status_code=202)
    def start_job_feishu_sync(job_id: str) -> dict[str, object]:
        try:
            return feishu_sync_manager.start(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except FeishuError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(job_id: str) -> StreamingResponse:
        try:
            job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

        async def stream():
            previous = ""
            while True:
                job = job_manager.get_job(job_id)
                payload = json.dumps(job, ensure_ascii=False)
                if payload != previous:
                    yield f"event: job_update\ndata: {payload}\n\n"
                    previous = payload
                if job["status"] in TERMINAL_STATES:
                    break
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream")

    static_directory = root / "frontend"
    if static_directory.exists():
        app.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
    return app
