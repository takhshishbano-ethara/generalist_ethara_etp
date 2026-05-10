from __future__ import annotations

import base64
import logging
import re
import threading
import time

import boto3
import docker
from docker.errors import ImageNotFound

from src.core.config import ECRConfig

log = logging.getLogger(__name__)

_ECR_TOKEN_LIFETIME = 43200


class ECRCredentials:
    __slots__ = ("username", "password", "registry", "expires_at_monotonic")

    def __init__(self, username: str, password: str, registry: str, expires_at_monotonic: float):
        self.username = username
        self.password = password
        self.registry = registry
        self.expires_at_monotonic = expires_at_monotonic


class ECRAuthManager:
    def __init__(self, config: ECRConfig):
        self._config = config
        self._credentials: ECRCredentials | None = None
        self._lock = threading.Lock()
        self._refreshing = False
        self._condition = threading.Condition(self._lock)
        self._ecr_client = boto3.client("ecr", region_name=config.region)

    def get_auth_config(self) -> dict[str, str]:
        creds = self._get_credentials()
        return {"username": creds.username, "password": creds.password}

    def invalidate(self) -> None:
        with self._lock:
            self._credentials = None

    def _get_credentials(self) -> ECRCredentials:
        with self._lock:
            while self._refreshing:
                self._condition.wait()
            if not self._needs_refresh():
                return self._credentials  # type: ignore[return-value]
            self._refreshing = True

        try:
            self._perform_refresh()
        finally:
            with self._lock:
                self._refreshing = False
                self._condition.notify_all()

        return self._credentials  # type: ignore[return-value]

    def _needs_refresh(self) -> bool:
        if self._credentials is None:
            return True
        refresh_at = self._credentials.expires_at_monotonic - self._config.refresh_buffer_seconds
        return time.monotonic() >= refresh_at

    def _perform_refresh(self) -> None:
        log.info("Refreshing ECR auth token (region=%s)", self._config.region)
        response = self._ecr_client.get_authorization_token()
        auth_data = response["authorizationData"][0]
        decoded = base64.b64decode(auth_data["authorizationToken"]).decode()
        username, password = decoded.split(":", 1)
        registry = auth_data["proxyEndpoint"].replace("https://", "")
        with self._lock:
            self._credentials = ECRCredentials(
                username=username,
                password=password,
                registry=registry,
                expires_at_monotonic=time.monotonic() + _ECR_TOKEN_LIFETIME,
            )


def resolve_image_uri(instance_id: str, config: ECRConfig) -> str:
    """Map instance_id to ECR image URI.

    "numpy__numpy-12345" → "<account>.dkr.ecr.<region>.amazonaws.com/<repo>/numpy_m_numpy:pr-12345"
    """
    match = re.search(r'-(\d+)$', instance_id)
    if match is None:
        raise ValueError(f"Invalid instance_id (no '-'): {instance_id}")

    pr_num = match.group(1)
    prefix = instance_id[:match.start()]

    if "__" not in prefix:
        raise ValueError(f"Invalid instance_id (no '__'): {instance_id}")

    org, repo = prefix.split("__", 1)
    image_name = f"{org}_m_{repo}"
    tag = f"pr-{pr_num}"

    registry = f"{config.account_id}.dkr.ecr.{config.region}.amazonaws.com"
    return f"{registry}/{config.repository}/{image_name}:{tag}"


class ECRImageManager:
    def __init__(self, auth_manager: ECRAuthManager, docker_client: docker.DockerClient | None = None):
        self._auth = auth_manager
        self._docker = docker_client or docker.from_env()
        self._pulled: set[str] = set()

    def ensure_image(self, image_uri: str, force: bool = False) -> None:
        if not force and image_uri in self._pulled:
            return
        if not force and self._image_exists_locally(image_uri):
            self._pulled.add(image_uri)
            return
        self._pull_image(image_uri)
        self._pulled.add(image_uri)

    def _image_exists_locally(self, image_uri: str) -> bool:
        try:
            self._docker.images.get(image_uri)
            return True
        except ImageNotFound:
            return False

    def _pull_image(self, image_uri: str) -> None:
        repo, tag = image_uri.rsplit(":", 1) if ":" in image_uri else (image_uri, "latest")
        auth_config = self._auth.get_auth_config()
        log.info("Pulling ECR image: %s", image_uri)
        try:
            self._docker.images.pull(repository=repo, tag=tag, auth_config=auth_config)
        except docker.errors.APIError as e:
            if "401" in str(e) or "unauthorized" in str(e).lower():
                log.warning("ECR auth expired, retrying with fresh token...")
                self._auth.invalidate()
                auth_config = self._auth.get_auth_config()
                self._docker.images.pull(repository=repo, tag=tag, auth_config=auth_config)
            else:
                raise
