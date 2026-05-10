from __future__ import annotations

import base64
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import ECRConfig
from src.rollout.ecr import ECRAuthManager, ECRImageManager, resolve_image_uri


class TestResolveImageUri:
    @pytest.fixture
    def config(self) -> ECRConfig:
        return ECRConfig(
            enabled=True,
            account_id="426628337772",
            region="ap-south-1",
            repository="rfp-coding-q1-tag",
        )

    def test_standard_instance_id(self, config):
        uri = resolve_image_uri("numpy__numpy-12345", config)
        assert uri == (
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/"
            "rfp-coding-q1-tag/numpy_m_numpy:pr-12345"
        )

    def test_hyphenated_repo(self, config):
        uri = resolve_image_uri("scikit-learn__scikit-learn-23456", config)
        assert uri == (
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/"
            "rfp-coding-q1-tag/scikit-learn_m_scikit-learn:pr-23456"
        )

    def test_complex_org_repo(self, config):
        uri = resolve_image_uri("amrex-codes__amrex-4238", config)
        assert uri == (
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/"
            "rfp-coding-q1-tag/amrex-codes_m_amrex:pr-4238"
        )

    def test_invalid_no_hyphen(self, config):
        with pytest.raises(ValueError, match="no '-'"):
            resolve_image_uri("numpynumpy12345", config)

    def test_invalid_no_double_underscore(self, config):
        with pytest.raises(ValueError, match="no '__'"):
            resolve_image_uri("numpy_numpy-12345", config)

    def test_custom_config(self):
        config = ECRConfig(
            account_id="123456789012",
            region="us-east-1",
            repository="my-repo",
        )
        uri = resolve_image_uri("org__repo-99", config)
        assert uri == "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo/org_m_repo:pr-99"


class TestECRAuthManager:
    @pytest.fixture
    def config(self) -> ECRConfig:
        return ECRConfig(enabled=True, region="ap-south-1", refresh_buffer_seconds=1800)

    @pytest.fixture
    def mock_boto3(self):
        token = base64.b64encode(b"AWS:secret-password-123").decode()
        mock_client = MagicMock()
        mock_client.get_authorization_token.return_value = {
            "authorizationData": [{
                "authorizationToken": token,
                "proxyEndpoint": "https://426628337772.dkr.ecr.ap-south-1.amazonaws.com",
            }]
        }
        with patch("src.rollout.ecr.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            yield mock_client

    def test_first_call_fetches_token(self, config, mock_boto3):
        mgr = ECRAuthManager(config)
        auth = mgr.get_auth_config()
        assert auth["username"] == "AWS"
        assert auth["password"] == "secret-password-123"
        mock_boto3.get_authorization_token.assert_called_once()

    def test_cached_token_reused(self, config, mock_boto3):
        mgr = ECRAuthManager(config)
        mgr.get_auth_config()
        mgr.get_auth_config()
        assert mock_boto3.get_authorization_token.call_count == 1

    def test_invalidate_forces_refresh(self, config, mock_boto3):
        mgr = ECRAuthManager(config)
        mgr.get_auth_config()
        mgr.invalidate()
        mgr.get_auth_config()
        assert mock_boto3.get_authorization_token.call_count == 2


class TestECRImageManager:
    @pytest.fixture
    def mock_docker(self):
        client = MagicMock()
        return client

    @pytest.fixture
    def mock_auth(self):
        auth = MagicMock()
        auth.get_auth_config.return_value = {"username": "AWS", "password": "tok"}
        return auth

    def test_image_exists_locally_skips_pull(self, mock_auth, mock_docker):
        mock_docker.images.get.return_value = MagicMock()
        mgr = ECRImageManager(mock_auth, mock_docker)
        mgr.ensure_image("some-image:tag")
        mock_docker.images.pull.assert_not_called()

    def test_image_not_local_pulls(self, mock_auth, mock_docker):
        from docker.errors import ImageNotFound
        mock_docker.images.get.side_effect = ImageNotFound("nope")
        mgr = ECRImageManager(mock_auth, mock_docker)
        mgr.ensure_image("registry/repo/img:tag")
        mock_docker.images.pull.assert_called_once_with(
            repository="registry/repo/img", tag="tag", auth_config={"username": "AWS", "password": "tok"}
        )

    def test_in_memory_cache_avoids_repeat(self, mock_auth, mock_docker):
        mock_docker.images.get.return_value = MagicMock()
        mgr = ECRImageManager(mock_auth, mock_docker)
        mgr.ensure_image("img:v1")
        mgr.ensure_image("img:v1")
        assert mock_docker.images.get.call_count == 1

    def test_force_bypasses_cache(self, mock_auth, mock_docker):
        mock_docker.images.get.return_value = MagicMock()
        mgr = ECRImageManager(mock_auth, mock_docker)
        mgr.ensure_image("img:v1")
        mock_docker.images.pull.assert_not_called()
        mgr.ensure_image("img:v1", force=True)
        mock_docker.images.pull.assert_called_once()
