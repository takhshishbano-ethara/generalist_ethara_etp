"""Google Drive upload service for Fenrir tasks.

The service is an AbstractModel so it can be obtained via
self.env["fenrir.drive.service"] and reused without instantiation overhead.

Configuration is stored in ir.config_parameter and edited through the
Fenrir → Configuration → Google Drive screen.
"""

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
        try:
            service.files().get(
                fileId=folder_id, fields="id, trashed",
                supportsAllDrives=True).execute()
            return True
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

        if reused:
            self._delete_folder_children(service, task_folder_id)

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
            folder_id = self._create_folder(service, dir_parts[-1], parent)
            folder_cache[dir_parts] = folder_id
            return folder_id

        # Always create the standard package sub-folders, even if no file lands
        # in them this run, so the Drive tree layout stays consistent.
        for base in task._EXPORT_BASE_DIRS:
            ensure_path((base,))

        for rel_path, content, mime, is_binary_upload, existing_s3_key in \
                task._collect_export_files():
            parts = rel_path.split("/")
            file_name = parts[-1]
            dir_parts = tuple(parts[:-1])
            parent_for_file = ensure_path(dir_parts)
            content_mime = mime or DEFAULT_FILE_MIME

            # Always upload the real file to Drive (resumable handles big sizes).
            self._upload_bytes(
                service, file_name, parent_for_file, content, content_mime)

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
            "Fenrir: uploaded task %s (Drive folder %s, S3 prefix %s)",
            task.code, task_folder_id, s3_prefix)
        return task_folder_id
