"""Regression tests for plan gap analysis false negatives on Django projects.

Each case reported an existing endpoint or model as missing, which made
``run-plan-audit`` send the task back to PLANNING:

1. ``include(<variable>)`` / imported lists / ``include((list, app))``
2. claims written relative to their mount point
3. "`/a` / `/b`" endpoint cells yielding a bare "/"
4. Django default table names (``<app_label>_<model>``)
5. models whose bases are ``AbstractUser`` or that only have Meta/fields
6. model cells whose notes backtick a column name; ``existing-modified``
7. ``to-be-created`` exempting only its own row
8. silent truncation at the repo-walk file cap
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import plan_gap_analysis  # noqa: E402
from plan_gap_analysis import (  # noqa: E402
    _cell_endpoints,
    _cell_model_name,
    _scan_code_files,
    analyze_api_contracts,
    analyze_data_model,
    findings_from_report,
    run_gap_analysis,
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _api_plan(rows: str) -> str:
    return (
        "# Plan\n\n## API Contracts\n\n"
        "| Endpoint | Method | Notes |\n|---|---|---|\n"
        f"{rows}\n"
    )


def _dm_plan(rows: str, header: str = "| Model | Scope |\n|---|---|\n") -> str:
    return f"# Plan\n\n## Data Model\n\n{header}{rows}\n"


def _endpoints(result: dict) -> list[str]:
    return [c["endpoint"] for c in result["unverified"]]


# ---------------------------------------------------------------------------
# 1. include(<variable>)
# ---------------------------------------------------------------------------

_ROOT_URLS = (
    "from django.urls import include, path\n"
    "from apps.accounts.urls import workspace_urlpatterns\n"
    "urlpatterns = [\n"
    '    path("api/v1/auth/", include("apps.accounts.urls")),\n'
    '    path("api/v1/workspaces/<uuid:w>/", include(workspace_urlpatterns)),\n'
    "]\n"
)
_ACCOUNTS_URLS = (
    "from django.urls import path\n"
    "from . import views\n"
    "urlpatterns = [\n"
    '    path("login", views.login),\n'
    "]\n"
    "workspace_urlpatterns = [\n"
    '    path("members", views.members),\n'
    "]\n"
)


def test_include_of_imported_list_gets_its_own_prefix(tmp_path: Path) -> None:
    _write(tmp_path, {"config/urls.py": _ROOT_URLS, "apps/accounts/urls.py": _ACCOUNTS_URLS})
    result = analyze_api_contracts(
        _api_plan(
            "| `/api/v1/workspaces/:id/members` | GET | |\n"
            "| `/api/v1/auth/login` | POST | |"
        ),
        tmp_path,
    )
    assert result["unverified"] == []
    # The list is mounted only under workspaces/, not under auth/.
    wrong = analyze_api_contracts(_api_plan("| `/api/v1/auth/members` | GET | |"), tmp_path)
    assert _endpoints(wrong) == ["/api/v1/auth/members"]


def test_include_of_same_module_list(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            'urlpatterns = [path("api/", include("apps.core.urls"))]\n'
        ),
        "apps/core/urls.py": (
            "from django.urls import include, path\n"
            'item_patterns = [path("history", v.history)]\n'
            'urlpatterns = [path("items/<int:pk>/", include(item_patterns))]\n'
        ),
    })
    result = analyze_api_contracts(_api_plan("| `/api/items/{id}/history` | GET | |"), tmp_path)
    assert result["unverified"] == []


def test_include_via_module_import_and_tuple_and_inline_list(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            "from apps.billing import urls as billing_urls\n"
            "from apps.chat import urls\n"
            "import apps.files.urls\n"
            "urlpatterns = [\n"
            '    path("billing/", include(billing_urls)),\n'
            '    path("chat/", include((urls.chat_patterns, "chat"), namespace="chat")),\n'
            '    path("files/", include(apps.files.urls)),\n'
            '    path("misc/", include([path("ping", v.ping)])),\n'
            "]\n"
        ),
        "apps/billing/urls.py": 'from django.urls import path\nurlpatterns = [path("invoices", v.i)]\n',
        "apps/chat/urls.py": 'from django.urls import path\nchat_patterns = [path("rooms", v.r)]\n',
        "apps/files/urls.py": 'from django.urls import path\nurlpatterns = [path("upload", v.u)]\n',
    })
    result = analyze_api_contracts(
        _api_plan(
            "| `/billing/invoices` | GET | |\n| `/chat/rooms` | GET | |\n"
            "| `/files/upload` | POST | |\n| `/misc/ping` | GET | |"
        ),
        tmp_path,
    )
    assert result["unverified"] == []


def test_list_wrapped_in_a_call_is_mounted(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            'urlpatterns = [path("api/", include("apps.core.urls"))]\n'
        ),
        "apps/core/urls.py": (
            "from django.urls import path\n"
            'api_patterns = [path("health", v.h)]\n'
            "urlpatterns = format_suffix_patterns(api_patterns)\n"
        ),
    })
    result = analyze_api_contracts(_api_plan("| `/api/health` | GET | |\n| `/v9/health` | GET | |"), tmp_path)
    # Mounted, so the unmounted tail match no longer rescues a wrong prefix.
    assert _endpoints(result) == ["/v9/health"]


def test_include_follows_a_relative_reexport(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            "from apps.api.urls import v2_patterns\n"
            'urlpatterns = [path("api/v2/", include(v2_patterns))]\n'
        ),
        "apps/api/urls.py": "from .routes import v2_patterns\n",
        "apps/api/routes.py": 'from django.urls import path\nv2_patterns = [path("status", v.s)]\n',
    })
    result = analyze_api_contracts(_api_plan("| `/api/v2/status` | GET | |"), tmp_path)
    assert result["unverified"] == []


# ---------------------------------------------------------------------------
# 2. Relative claims against mounted routes
# ---------------------------------------------------------------------------

def test_relative_claim_matches_tail_of_mounted_route(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            'urlpatterns = [path("api/v1/", include("apps.files.urls"))]\n'
        ),
        "apps/files/urls.py": (
            "from django.urls import path\n"
            'urlpatterns = [path("attachments/initiate", v.init), path("reinitiate", v.r)]\n'
        ),
    })
    result = analyze_api_contracts(
        _api_plan("| `/attachments/initiate` | POST | |\n| `/initiate` | POST | |"), tmp_path,
    )
    assert _endpoints(result) == []
    # Segment-boundary suffix only: "/tiate" is a raw-string tail of
    # ".../initiate" but not a segment tail; an all-param claim pins nothing.
    nope = analyze_api_contracts(_api_plan("| `/tiate` | POST | |\n| `/:id` | GET | |"), tmp_path)
    assert _endpoints(nope) == ["/tiate", "/:id"]


# ---------------------------------------------------------------------------
# 3. "`/a` / `/b`" cells
# ---------------------------------------------------------------------------

def test_cell_with_slash_separated_paths_has_no_bare_root() -> None:
    assert _cell_endpoints("`/a` / `/b`") == ["/a", "/b"]
    assert _cell_endpoints("/a / /b, /c") == ["/a", "/b", "/c"]
    assert _cell_endpoints("`/`") == ["/"]
    assert _cell_endpoints("`/api/x/{id}`") == ["/api/x/{id}"]


def test_slash_separated_cell_is_verified(tmp_path: Path) -> None:
    _write(tmp_path, {"src/app.js": "app.get('/api/a', h);\napp.get('/api/b', h);\n"})
    result = analyze_api_contracts(_api_plan("| `/api/a` / `/api/b` | GET | |"), tmp_path)
    assert result["claimed_endpoints"] == 2
    assert result["unverified"] == []


# ---------------------------------------------------------------------------
# 4. + 5. Django default table names and model detection
# ---------------------------------------------------------------------------

def test_django_default_table_names_and_abstract_user(tmp_path: Path) -> None:
    _write(tmp_path, {
        "backend/accounts/models.py": (
            "from django.contrib.auth.models import AbstractUser\n"
            "from django.db import models\n"
            "class User(AbstractUser):\n    pass\n"
        ),
        "backend/chat/models/__init__.py": "",
        "backend/chat/models/conversation.py": (
            "from django.db import models\n"
            "class Conversation(TimeStamped):\n"
            "    title = models.CharField(max_length=10)\n"
            "class Tag(Mixin):\n"
            "    class Meta:\n        app_label = 'labels'\n"
            "class Common(models.Model):\n"
            "    class Meta:\n        abstract = True\n"
        ),
    })
    result = analyze_data_model(
        _dm_plan(
            "| accounts_user | existing |\n| User | existing |\n"
            "| chat_conversation | existing |\n| Conversation | existing |\n"
            "| labels_tag | existing |\n| chat_common | existing |"
        ),
        tmp_path,
    )
    assert result["unverified"] == ["chat_common"]  # abstract: no table
    assert "accounts_user" in result["verified"]
    assert "labels_tag" in result["verified"]


def test_plain_class_is_not_a_model(tmp_path: Path) -> None:
    _write(tmp_path, {"app/models.py": "from django.db import models\nclass Helper(object):\n    x = 1\n"})
    result = analyze_data_model(_dm_plan("| Helper | existing |"), tmp_path)
    assert result["unverified"] == ["helper"]


# ---------------------------------------------------------------------------
# 6. Leading identifier and existing-modified
# ---------------------------------------------------------------------------

def test_cell_model_name_uses_leading_identifier() -> None:
    assert _cell_model_name("Conversation (add `triage_state`)") == "conversation"
    assert _cell_model_name("`Organization` (existing, app/models.py)") == "organization"
    assert _cell_model_name("models.Membership") == "membership"
    assert _cell_model_name("**Invite**") == "invite"
    assert _cell_model_name("—") == ""
    assert _cell_model_name("— see `Ledger`") == "ledger"


def test_existing_modified_row_checks_the_model_not_the_new_column(tmp_path: Path) -> None:
    _write(tmp_path, {
        "chat/models.py": (
            "from django.db import models\n"
            "class Conversation(models.Model):\n    title = models.CharField()\n"
        ),
    })
    plan = _dm_plan(
        "| Conversation (add `triage_state`) | existing-modified: new column to-be-created |\n"
    )
    result = analyze_data_model(plan, tmp_path)
    assert result["unverified"] == []
    assert result["existing_modified"] == ["conversation"]
    assert result["to_be_created"] == []
    assert result["verified"] == ["conversation"]


def test_existing_modified_model_that_is_missing_still_fails(tmp_path: Path) -> None:
    _write(tmp_path, {"app/models.py": "x = 1\n"})
    result = analyze_data_model(_dm_plan("| Ghost | existing-modified |"), tmp_path)
    assert result["unverified"] == ["ghost"]


# ---------------------------------------------------------------------------
# 7. to-be-created applies to every row naming the model
# ---------------------------------------------------------------------------

def test_to_be_created_exempts_every_row_for_that_model(tmp_path: Path) -> None:
    _write(tmp_path, {"app/models.py": "x = 1\n"})
    plan = _dm_plan(
        "| Invite | id | to-be-created |\n"
        "| Invite | email | |\n"
        "| `Invite` | token | |\n"
        "| Ghost | id | |",
        header="| Table | Column | Scope |\n|---|---|---|\n",
    )
    result = analyze_data_model(plan, tmp_path)
    assert result["to_be_created"] == ["invite"]
    assert result["unverified"] == ["ghost"]


# ---------------------------------------------------------------------------
# 8. File-cap truncation is reported and not a hard failure
# ---------------------------------------------------------------------------

def test_scan_code_files_reports_truncation(tmp_path: Path) -> None:
    for i in range(4):
        _write(tmp_path, {f"src/m{i}.py": "x = 1\n"})
    files, truncated = _scan_code_files(tmp_path, {".py"}, limit=4)
    assert len(files) == 4 and truncated is False
    files, truncated = _scan_code_files(tmp_path, {".py"}, limit=3)
    assert len(files) == 3 and truncated is True


def test_truncated_scan_downgrades_not_found_to_inconclusive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DYNOS_GAP_CACHE", "0")
    monkeypatch.setattr(plan_gap_analysis, "_SCAN_FILE_LIMIT", 2)
    monkeypatch.setattr(
        plan_gap_analysis, "_scan_code_files",
        lambda root, ext, limit=2: (sorted((root / "src").glob("*.py"))[:2], True),
    )
    for i in range(3):
        _write(tmp_path, {f"src/m{i}.py": "x = 1\n"})
    task_dir = tmp_path / ".dynos" / "task-1"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(
        _api_plan("| `/api/ghost` | GET | |") + "\n" + _dm_plan("| Ghost | existing |")
    )

    report = run_gap_analysis(tmp_path, task_dir)

    assert report["scan_truncated"] is True
    assert report["files_scanned"] == 2 and report["scan_limit"] == 2
    assert report["warnings"]
    api, dm = report["api_contracts"], report["data_model"]
    assert api["unverified"] == [] and _endpoints({"unverified": api["unverified_inconclusive"]}) == ["/api/ghost"]
    assert dm["unverified"] == [] and dm["unverified_inconclusive"] == ["ghost"]
    assert api["scan_truncated"] is True and dm["warnings"]
    assert findings_from_report(report) == []


def test_untruncated_scan_still_fails_missing_claims(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DYNOS_GAP_CACHE", "0")
    _write(tmp_path, {"src/m.py": "x = 1\n"})
    task_dir = tmp_path / ".dynos" / "task-1"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(_api_plan("| `/api/ghost` | GET | |"))
    report = run_gap_analysis(tmp_path, task_dir)
    assert "scan_truncated" not in report
    assert report["api_contracts"]["scan_truncated"] is False
    assert _endpoints(report["api_contracts"]) == ["/api/ghost"]
    assert len(findings_from_report(report)) == 1


# ---------------------------------------------------------------------------
# 9. relative endpoint cells (7.6.0 field report): "companies/{id}" used to
#    be cut at its first slash to "/{company_id}", which nothing matches.
# ---------------------------------------------------------------------------

def test_relative_cell_keeps_its_first_segment() -> None:
    assert _cell_endpoints("companies/{company_id}") == ["/companies/{company_id}"]
    assert _cell_endpoints("`contacts/{contact_id}`") == ["/contacts/{contact_id}"]
    assert _cell_endpoints("outreach/sequences/{sequence_id}/mode") == ["/outreach/sequences/{sequence_id}/mode"]
    assert _cell_endpoints("`/a` / `/b`") == ["/a", "/b"]
    assert _cell_endpoints("companies/{id}, contacts/{id}") == ["/companies/{id}", "/contacts/{id}"]
    assert _cell_endpoints("https://api.example.com/v1/items") == ["/v1/items"]
    assert _cell_endpoints("/") == ["/"]


def _workspace_project(root: Path) -> None:
    _write(root, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            "urlpatterns = [\n"
            "    path('api/v1/workspaces/<uuid:workspace_id>/', include('apps.crm.urls')),\n"
            "    path('api/v1/workspaces/<uuid:workspace_id>/outreach/', include('apps.outreach.urls')),\n"
            "]\n"
        ),
        "apps/crm/urls.py": (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [\n"
            "    path('companies/<uuid:company_id>', views.company_detail),\n"
            "    path('contacts/<uuid:contact_id>', views.contact_detail),\n"
            "]\n"
        ),
        "apps/outreach/urls.py": (
            "from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path('sequences/<uuid:sequence_id>/mode', views.mode)]\n"
        ),
    })


def test_two_segment_relative_claims_match_included_routes(tmp_path: Path) -> None:
    _workspace_project(tmp_path)
    plan = _api_plan(
        "| companies/{company_id} | PATCH | |\n"
        "| contacts/{contact_id} | PATCH | |\n"
        "| outreach/sequences/{sequence_id}/mode | PATCH | |\n"
        "| workspaces/{workspace_id}/companies/{company_id} | PATCH | |\n"
        "| /api/v1/workspaces/{workspace_id}/contacts/{contact_id} | PATCH | |"
    )
    result = analyze_api_contracts(plan, tmp_path)
    assert result["unverified"] == [], _endpoints(result)
    assert result["verified"] == 5


def test_relative_claim_with_wrong_literal_still_fails(tmp_path: Path) -> None:
    _workspace_project(tmp_path)
    result = analyze_api_contracts(_api_plan("| invoices/{invoice_id} | PATCH | |"), tmp_path)
    assert _endpoints(result) == ["/invoices/{invoice_id}"]


def test_bare_param_claim_does_not_suffix_match(tmp_path: Path) -> None:
    _workspace_project(tmp_path)
    result = analyze_api_contracts(_api_plan("| /{company_id} | PATCH | |"), tmp_path)
    assert _endpoints(result) == ["/{company_id}"]


# ---------------------------------------------------------------------------
# 10. DRF routers mounted through include()
# ---------------------------------------------------------------------------

def test_drf_router_urls_are_mounted(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            "urlpatterns = [path('api/workspaces/<int:workspace_id>/', include('apps.crm.urls'))]\n"
        ),
        "apps/crm/urls.py": (
            "from django.urls import include, path\n"
            "from rest_framework.routers import DefaultRouter\n"
            "from rest_framework_nested.routers import NestedSimpleRouter\n"
            "from . import views\n"
            "router = DefaultRouter()\n"
            "router.register(r'companies', views.CompanyViewSet)\n"
            "contacts = NestedSimpleRouter(router, r'companies', lookup='company')\n"
            "contacts.register(r'contacts', views.ContactViewSet)\n"
            "urlpatterns = [path('', include(router.urls))] + contacts.urls\n"
        ),
    })
    plan = _api_plan(
        "| companies/{company_id} | PATCH | |\n"
        "| /api/workspaces/{workspace_id}/companies | GET | |\n"
        "| companies/{company_id}/contacts/{contact_id} | PATCH | |"
    )
    result = analyze_api_contracts(plan, tmp_path)
    assert result["unverified"] == [], _endpoints(result)


def test_drf_router_imported_from_another_module(tmp_path: Path) -> None:
    _write(tmp_path, {
        "config/urls.py": (
            "from django.urls import include, path\n"
            "from apps.crm.routers import router\n"
            "urlpatterns = [path('api/', include(router.urls))]\n"
        ),
        "apps/crm/routers.py": (
            "from rest_framework.routers import SimpleRouter\n"
            "from . import views\n"
            "router = SimpleRouter()\n"
            "router.register('companies', views.CompanyViewSet)\n"
        ),
    })
    result = analyze_api_contracts(_api_plan("| /api/companies/{pk} | PATCH | |"), tmp_path)
    assert result["unverified"] == [], _endpoints(result)
