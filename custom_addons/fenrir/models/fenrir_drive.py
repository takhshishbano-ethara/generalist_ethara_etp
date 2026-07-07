"""Google Drive upload service for Fenrir tasks.

The service is an AbstractModel so it can be obtained via
self.env["fenrir.drive.service"] and reused without instantiation overhead.

Configuration is stored in ir.config_parameter and edited through the
Fenrir → Configuration → Google Drive screen.
"""

import hashlib
import io
import json
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)

PARAM_SERVICE_ACCOUNT = "fenrir.drive.service_account_json"
PARAM_PARENT_FOLDER = "fenrir.drive.parent_folder_id"
PARAM_OAUTH_CLIENT_ID = "fenrir.drive.oauth_client_id"
PARAM_OAUTH_CLIENT_SECRET = "fenrir.drive.oauth_client_secret"
PARAM_OAUTH_REFRESH_TOKEN = "fenrir.drive.oauth_refresh_token"

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_FILE_MIME = "application/octet-stream"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class FenrirDriveService(models.AbstractModel):
    _name = "fenrir.drive.service"
    _description = "Fenrir — Google Drive Upload Service"

    # ── Config + client ──────────────────────────────────────────────────
    def _build_client(self):
        """Return (drive_v3_service, parent_folder_id).

        Prefers OAuth-user credentials when a refresh token is configured
        (works for personal / free Gmail Drives). Falls back to service
        account auth (only works for Shared Drives).
        """
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        parent_id = (config.parent_folder_id or "").strip()
        if not parent_id:
            raise UserError(_(
                "Google Drive parent folder ID is not configured.\n"
                "Set it under Fenrir → Configuration → Google Drive."))

        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise UserError(_(
                "Python packages 'google-api-python-client' and 'google-auth' "
                "are not installed in the Odoo environment.\n"
                "Run:  pip install google-api-python-client google-auth "
                "google-auth-oauthlib"
            )) from exc

        if config.auth_method == "oauth":
            creds = self._oauth_credentials(config)
        else:
            creds = self._service_account_credentials(config)

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service, parent_id

    @staticmethod
    def _oauth_credentials(config):
        refresh_token = (config.oauth_refresh_token or "").strip()
        client_id = (config.oauth_client_id or "").strip()
        client_secret = (config.oauth_client_secret or "").strip()
        if not refresh_token:
            raise UserError(_(
                "OAuth refresh token is missing. Run scripts/authorize_drive.py "
                "and paste the result under Fenrir → Configuration → Google Drive."))
        if not (client_id and client_secret):
            raise UserError(_(
                "OAuth client_id / client_secret are missing. Fill them under "
                "Fenrir → Configuration → Google Drive."))
        from google.oauth2.credentials import Credentials
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=DRIVE_SCOPES,
        )

    @staticmethod
    def _service_account_credentials(config):
        raw_json = (config.service_account_json or "").strip()
        if not raw_json:
            raise UserError(_(
                "Service Account JSON is not configured.\n"
                "Either switch auth method to OAuth (works on personal Gmail) "
                "or paste a service account JSON key under Fenrir → "
                "Configuration → Google Drive."))
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise UserError(_(
                "The configured service account JSON is not valid JSON: %s"
            ) % exc) from exc
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_info(
            info, scopes=DRIVE_SCOPES)

    # ── Low-level Drive helpers ──────────────────────────────────────────
    @staticmethod
    def _folder_exists(service, folder_id):
        """True only if the folder is reachable AND not in Drive Trash.

        A trashed folder still resolves via files.get(), but writing
        children into it makes them invisible to the standard listing
        query (`trashed = false`). Treating trashed as non-existent
        forces _upload_task_inner to fall through to creating a fresh
        folder instead of reusing the trashed one.
        """
        try:
            meta = service.files().get(
                fileId=folder_id, fields="id, trashed",
                supportsAllDrives=True).execute()
            return not meta.get("trashed")
        except Exception:  # noqa: BLE001 — Drive HttpError + any net failure
            return False

    @staticmethod
    def _create_folder(service, name, parent_id):
        body = {
            "name": name,
            "mimeType": DRIVE_FOLDER_MIME,
            "parents": [parent_id],
        }
        result = service.files().create(
            body=body, fields="id", supportsAllDrives=True).execute()
        return result["id"]

    @staticmethod
    def _delete_folder_children(service, folder_id):
        """Trash every direct child of folder_id (recursive cleanup before re-upload)."""
        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for child in resp.get("files", []):
                service.files().delete(
                    fileId=child["id"], supportsAllDrives=True).execute()
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    @staticmethod
    def _find_folders_by_name(service, name, parent_id):
        """Return ids of all non-trashed folders named `name` under parent_id."""
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"name = '{safe}' and '{parent_id}' in parents "
             f"and mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false")
        ids, page_token = [], None
        while True:
            resp = service.files().list(
                q=q, spaces="drive",
                fields="nextPageToken, files(id)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            ids.extend(f["id"] for f in resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    @staticmethod
    def _trash_folder(service, folder_id):
        """Move a folder to Drive trash (recoverable for ~30 days)."""
        service.files().update(
            fileId=folder_id, body={"trashed": True},
            supportsAllDrives=True).execute()

    def list_task_subfolders(self):
        """Return {folder_name: folder_id} for every immediate sub-folder
        of the configured Drive parent folder. Used by the All-Tasks Excel
        export to discover per-task Drive folders by code, even when
        task.drive_folder_id was never written back to Odoo.
        Empty dict on any Drive failure (export still proceeds)."""
        try:
            service, parent_id = self._build_client()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Fenrir Drive: list_task_subfolders — client init failed.",
                exc_info=True)
            return {}
        if not parent_id:
            return {}

        result = {}
        page_token = None
        while True:
            try:
                resp = service.files().list(
                    q=(f"'{parent_id}' in parents and trashed = false "
                       f"and mimeType = '{DRIVE_FOLDER_MIME}'"),
                    fields="nextPageToken, files(id, name)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Fenrir Drive: list_task_subfolders page failed.",
                    exc_info=True)
                return result
            for f in resp.get("files", []):
                result[f["name"]] = f["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return result

    def list_files_with_urls(self, root_folder_id):
        """Walk a Drive folder tree and return {relative_path: webViewLink}.

        Used by the All-Tasks Excel export to put real Drive URLs in the
        attachment columns instead of just file names. Returns an empty dict
        if the root folder cannot be reached (so the export still proceeds).

        Both files AND folders are recorded — folders are keyed by their
        relative path with no trailing slash (e.g. ``"data"`` or
        ``"submissions/seller_1"``), so the export can put a single folder
        URL in the Data / Resources / Environment / Tests columns instead of
        a list of individual file URLs.
        """
        if not root_folder_id:
            return {}
        try:
            service, _parent_id = self._build_client()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Fenrir Drive: list_files_with_urls — client init failed; "
                "skipping URL lookup.", exc_info=True)
            return {}

        # Detect a trashed root folder up front. The walk's children query
        # uses `trashed = false`, which silently returns 0 results for a
        # trashed parent — indistinguishable from "folder exists and is
        # genuinely empty". A single get() here surfaces the real cause
        # in the log so re-uploads / Drive Trash restores are obvious.
        try:
            meta = service.files().get(
                fileId=root_folder_id,
                fields="id, name, trashed",
                supportsAllDrives=True,
            ).execute()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Fenrir Drive: cannot read folder %s metadata; "
                "URL lookup will return empty.",
                root_folder_id, exc_info=True)
            return {}
        if meta.get("trashed"):
            _logger.warning(
                "Fenrir Drive: folder %s (%r) is in Drive Trash — "
                "restore it or re-upload the task to populate URLs.",
                root_folder_id, meta.get("name"))
            return {}

        result = {}

        def walk(folder_id, prefix):
            page_token = None
            while True:
                try:
                    resp = service.files().list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=("nextPageToken, "
                                "files(id, name, webViewLink, mimeType)"),
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    ).execute()
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Fenrir Drive: list failed for folder %s",
                        folder_id, exc_info=True)
                    return
                for f in resp.get("files", []):
                    name = f["name"]
                    path = f"{prefix}{name}"
                    if f["mimeType"] == DRIVE_FOLDER_MIME:
                        result[path] = f.get("webViewLink") or ""
                        walk(f["id"], f"{path}/")
                    else:
                        result[path] = f.get("webViewLink") or ""
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        walk(root_folder_id, "")
        return result

    @staticmethod
    def _upload_bytes(service, name, parent_id, data, mime=DEFAULT_FILE_MIME):
        """Upload bytes to Drive using resumable chunked upload.

        resumable=True with a 10 MB chunksize lets us push files of any size
        without keeping the request body in memory all at once. Simple
        (non-resumable) uploads cap out around a few hundred MB.
        """
        from googleapiclient.http import MediaIoBaseUpload
        body = {"name": name, "parents": [parent_id]}
        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype=mime,
            resumable=True, chunksize=10 * 1024 * 1024)
        request = service.files().create(
            body=body, media_body=media, fields="id",
            supportsAllDrives=True)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        return response["id"]

    @staticmethod
    def _update_file_bytes(service, file_id, data, mime):
        """Replace an existing Drive file's content in place.

        Preserves the file's ID and webViewLink so external links
        and bookmarks survive a re-approve.
        """
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype=mime,
            resumable=True, chunksize=10 * 1024 * 1024)
        request = service.files().update(
            fileId=file_id, media_body=media,
            supportsAllDrives=True)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        return response["id"]

    @staticmethod
    def _walk_existing_files(service, folder_id):
        """Walk the task folder tree on Drive, return {rel_path: {id, md5}}.

        Used by delta-sync re-approve to decide which files actually
        need a network write. md5Checksum is empty for Google-native
        types (Docs/Sheets) — those will always be treated as 'changed'
        and re-uploaded. Folders are not included in the result.
        """
        result = {}

        def walk(parent_id, prefix):
            page_token = None
            while True:
                try:
                    resp = service.files().list(
                        q=f"'{parent_id}' in parents and trashed = false",
                        fields=("nextPageToken, files(id, name, "
                                "mimeType, md5Checksum)"),
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    ).execute()
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Fenrir Drive: delta walk failed under folder %s",
                        parent_id, exc_info=True)
                    return
                for f in resp.get("files", []):
                    name = f["name"]
                    path = f"{prefix}{name}"
                    if f.get("mimeType") == DRIVE_FOLDER_MIME:
                        walk(f["id"], f"{path}/")
                    else:
                        result[path] = {
                            "id": f["id"],
                            "md5": f.get("md5Checksum") or "",
                        }
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        walk(folder_id, "")
        return result

    @staticmethod
    def _find_subfolder_by_name(service, parent_id, name):
        """Return the first non-trashed subfolder id under parent_id
        whose name matches `name`, or None. Used by delta-sync's
        ensure_path so we reuse the existing subfolder instead of
        creating a duplicate.
        """
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"name = '{safe}' and '{parent_id}' in parents "
             f"and mimeType = '{DRIVE_FOLDER_MIME}' and trashed = false")
        try:
            resp = service.files().list(
                q=q, spaces="drive",
                fields="files(id)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except Exception:  # noqa: BLE001
            return None
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    # ── Public: upload a task package ────────────────────────────────────
    def upload_task(self, task):
        """Upload one fenrir.task's package to Drive.

        Creates (or re-uses) a <TASK_CODE> folder under the configured parent,
        wipes its contents on re-upload, then re-creates the full folder tree
        from task._collect_export_files().
        """
        task.ensure_one()
        try:
            return self._upload_task_inner(task)
        except UserError:
            raise
        except Exception as exc:
            msg = str(exc)
            if "storageQuotaExceeded" in msg or "storage quota" in msg.lower():
                raise UserError(_(
                    "Google Drive rejected the upload because service accounts "
                    "have no storage quota of their own.\n\n"
                    "Fix: the configured parent folder must live inside a "
                    "Shared Drive (Google Workspace feature), not in a "
                    "personal 'My Drive'.\n\n"
                    "Steps:\n"
                    "  1. Open Google Drive → 'Shared drives' in the left rail "
                    "→ create a new shared drive (or pick an existing one).\n"
                    "  2. Add the service account email as a Manager.\n"
                    "  3. Create the parent folder inside that shared drive.\n"
                    "  4. Update the Parent Folder ID in Fenrir → Configuration "
                    "→ Google Drive.\n\n"
                    "Original error: %s") % msg) from exc
            raise

    def _upload_task_inner(self, task):
        service, parent_id = self._build_client()
        s3 = self.env["fenrir.s3.service"]
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        s3_folder = (config.s3_folder or "").strip().strip("/")
        s3_prefix = f"{s3_folder}/{task.code or f'task_{task.id}'}" if s3_folder \
            else f"{task.code or f'task_{task.id}'}"

        folder_name = task.code or f"task_{task.id}"

        # Resolve the task's Drive folder idempotently by name. A previous
        # upload that failed mid-way leaves a real folder in Drive but rolls
        # back the drive_folder_id write, so on retry we look the folder up by
        # name instead of blindly creating a duplicate "ghost" folder.
        existing = self._find_folders_by_name(service, folder_name, parent_id)
        stored = (task.drive_folder_id or "").strip()

        if stored and stored in existing:
            task_folder_id, reused = stored, True
        elif stored and self._folder_exists(service, stored):
            task_folder_id, reused = stored, True   # valid but renamed/moved
        elif existing:
            task_folder_id, reused = existing[0], True
        else:
            task_folder_id = self._create_folder(service, folder_name, parent_id)
            reused = False

        # Trash any other same-named folders under the parent — these are
        # ghost duplicates left behind by an earlier interrupted upload.
        for ghost_id in existing:
            if ghost_id != task_folder_id:
                try:
                    self._trash_folder(service, ghost_id)
                    _logger.info(
                        "Fenrir: trashed duplicate Drive folder %s for %s",
                        ghost_id, folder_name)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "Fenrir: could not trash duplicate Drive folder %s: %s",
                        ghost_id, exc)

        # On re-approve (reused=True), walk the existing Drive tree once
        # so we can md5-diff each local file against what's already there.
        # First-time approve (reused=False) skips the walk and uploads
        # everything fresh — that path is unchanged.
        existing_files = (
            self._walk_existing_files(service, task_folder_id)
            if reused else {})

        # NOTE: we no longer wholesale-wipe the task's S3 prefix here —
        # attachments are pushed to S3 at attach time (see
        # fenrir.task.attachment._maybe_push_to_s3) and the wipe would
        # erase those before we could restore them. Re-uploads overwrite
        # the same keys (deterministic naming) so duplicates don't pile up.

        folder_cache = {(): task_folder_id}

        def ensure_path(dir_parts):
            if dir_parts in folder_cache:
                return folder_cache[dir_parts]
            parent = ensure_path(dir_parts[:-1])
            # On re-approve, reuse the existing subfolder if present so
            # we don't end up with duplicate "resources/" "resources (1)/"
            # folders piling up across re-approves.
            existing_id = (
                self._find_subfolder_by_name(service, parent, dir_parts[-1])
                if reused else None)
            folder_id = existing_id or self._create_folder(
                service, dir_parts[-1], parent)
            folder_cache[dir_parts] = folder_id
            return folder_id

        # Always create the standard package sub-folders, even if no file lands
        # in them this run, so the Drive tree layout stays consistent.
        for base in task._EXPORT_BASE_DIRS:
            ensure_path((base,))

        drive_last = task.drive_last_uploaded_at
        unchanged = updated = created = 0
        for (rel_path, content_loader, mime, is_binary_upload,
             existing_s3_key, source_mtime) in task._collect_export_files():
            parts = rel_path.split("/")
            file_name = parts[-1]
            dir_parts = tuple(parts[:-1])
            content_mime = mime or DEFAULT_FILE_MIME

            existing_entry = existing_files.get(rel_path)

            # Fast path — Drive already has this file AND the source record
            # hasn't been touched since the previous successful approve.
            # Skip without reading bytes (avoids multi-GB S3 downloads for
            # large attachments that haven't changed).
            if (existing_entry and reused and source_mtime and drive_last
                    and source_mtime <= drive_last):
                unchanged += 1
                continue

            # Otherwise we need the bytes — either to md5-check or upload.
            try:
                content = content_loader()
            except Exception as exc:
                raise UserError(_(
                    "Could not read file '%s' for task %s while approving — the "
                    "underlying content may be missing (e.g. deleted from S3). "
                    "Original error: %s"
                ) % (rel_path, task.code, exc)) from exc
            local_md5 = hashlib.md5(content).hexdigest()

            if existing_entry and existing_entry.get("md5") == local_md5:
                # Drive's copy matches — skip both Drive AND S3.
                unchanged += 1
                continue

            parent_for_file = ensure_path(dir_parts)

            if existing_entry:
                # Same path, different content — update in place so the
                # file's Drive ID and webViewLink stay valid for any
                # external bookmarks / sheet references.
                self._update_file_bytes(
                    service, existing_entry["id"], content, content_mime)
                updated += 1
            else:
                # New file (first-time approve, or a file added since the
                # previous approve) — upload fresh.
                self._upload_bytes(
                    service, file_name, parent_for_file, content, content_mime)
                created += 1

            # Binary uploads also go to S3 for backup / external pipeline access.
            # If S3 fails, log but don't block Drive — Drive copy is authoritative.
            # Skip when the file was already pushed at attach time.
            if is_binary_upload and not existing_s3_key:
                s3_key = f"{s3_prefix}/{rel_path}"
                try:
                    s3.upload_bytes(s3_key, content, content_mime)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "Fenrir: S3 mirror failed for %s (Drive copy OK): %s",
                        s3_key, exc)

        task.write({
            "drive_folder_id": task_folder_id,
            "drive_last_uploaded_at": fields.Datetime.now(),
        })
        _logger.info(
            "Fenrir: uploaded task %s (Drive folder %s, S3 prefix %s) "
            "— delta: %s unchanged, %s updated, %s created",
            task.code, task_folder_id, s3_prefix,
            unchanged, updated, created)
        return task_folder_id
