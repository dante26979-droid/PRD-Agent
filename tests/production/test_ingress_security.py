from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_ingress_applies_bounded_per_ip_rejection() -> None:
    config = (
        REPOSITORY_ROOT / "infra/production/ingress/default.conf.template"
    ).read_text(encoding="utf-8")

    assert (
        "limit_req_zone $binary_remote_addr zone=prd_public:10m rate=5r/s;"
        in config
    )
    assert "limit_req_zone $binary_remote_addr zone=prd_api:10m rate=5r/s;" in config
    assert (
        "limit_conn_zone $binary_remote_addr zone=prd_connections:10m;" in config
    )
    assert "limit_req_status 429;" in config
    assert "limit_conn_status 429;" in config
    assert "limit_conn prd_connections 20;" in config
    assert "limit_req zone=prd_api burst=10 nodelay;" in config
    assert "limit_req zone=prd_public burst=30 nodelay;" in config


def test_ingress_container_keeps_existing_security_boundary() -> None:
    compose = (
        REPOSITORY_ROOT / "infra/production/docker-compose.go.yml"
    ).read_text(encoding="utf-8")
    ingress_service = compose.split("  ingress:", 1)[1].split(
        "  backup-agent:", 1
    )[0]

    assert 'user: "101:101"' in ingress_service
    assert "read_only: true" in ingress_service
    assert "no-new-privileges:true" in ingress_service
    assert "cap_drop:\n      - ALL" in ingress_service
    assert "privileged: true" not in ingress_service
    assert "NET_ADMIN" not in ingress_service


def test_production_control_plane_forwards_rollout_policy_to_writers() -> None:
    compose = (
        REPOSITORY_ROOT / "infra/production/docker-compose.go.yml"
    ).read_text(encoding="utf-8")
    api_service = compose.split("  go-api:", 1)[1].split(
        "  go-maintenance:", 1
    )[0]
    maintenance_service = compose.split("  go-maintenance:", 1)[1].split(
        "  go-capability:", 1
    )[0]

    for service in (api_service, maintenance_service):
        assert "PRD_AGENT_DEFAULT_WORKFLOW_VERSION" in service
        assert "PRD_AGENT_ROLLOUT_POLICY_VERSION" in service
        assert "PRD_AGENT_V4_CANARY_BASIS_POINTS" in service
        assert "PRD_AGENT_V4_INTERNAL_IDENTITIES" in service
