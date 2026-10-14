"""The parts of the ``openshell`` CLI's JSON output that openvigie reads (PRD D12, D13).

Strict, frozen and bounded like every boundary model, with one difference: unknown fields are
ignored rather than rejected, because OpenShell adds fields between releases and a new field must
not break the inventory. Fields openvigie doesn't use are not modelled (``config_revision``, for
example, exceeds 2**53).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Name = Annotated[str, Field(max_length=256)]
Text = Annotated[str, Field(max_length=4096)]
Many = 1024  # bound on every list and map


class OpenShellModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)


class Admission(OpenShellModel):
    policy_hash: Annotated[str, Field(max_length=128)] = ""
    state: Name = ""  # "accepted" once the gateway took the configuration


class Sandbox(OpenShellModel):
    """One entry of ``sandbox list -o json`` or the output of ``sandbox get -o json``."""

    id: Name
    name: Name
    workspace: Name = "default"
    phase: Name = ""  # Provisioning | Ready | Stopping | Stopped | Starting | Completed | Error ...
    labels: Annotated[dict[Name, Text], Field(max_length=Many)] = {}
    current_policy_version: int | None = None
    policy_source: Name | None = None  # "sandbox" or "global"; only in `sandbox get`
    configuration_admission: Admission | None = None
    main_process_started_at_ms: int | None = None  # only in `sandbox get`
    restart_count: int | None = None
    resource_version: int | None = None  # changes whenever the sandbox record changes


class SandboxPage(OpenShellModel):
    sandboxes: Annotated[list[Sandbox], Field(max_length=Many)] = []
    next_page_token: Annotated[str, Field(max_length=1024)] = ""


class Endpoint(OpenShellModel):
    host: Name | None = None  # None when only allowed_ips is set
    port: int | None = None
    ports: Annotated[list[int], Field(max_length=Many)] = []
    path: Text | None = None
    protocol: Name | None = None  # rest | websocket | graphql | mcp | json-rpc | tcp
    access: Name | None = None  # read-only | read-write | full
    enforcement: Name | None = None  # enforce | audit (OpenShell's default is audit)
    allowed_ips: Annotated[list[Name], Field(max_length=Many)] = []


class Binary(OpenShellModel):
    path: Text


class NetworkRule(OpenShellModel):
    name: Name = ""
    endpoints: Annotated[list[Endpoint], Field(max_length=Many)] = []
    binaries: Annotated[list[Binary], Field(max_length=Many)] = []


class PolicyBody(OpenShellModel):
    network_policies: Annotated[dict[Name, NetworkRule], Field(max_length=Many)] = {}


class Policy(OpenShellModel):
    """``policy get <sandbox> --base -o json``: the policy set for the sandbox."""

    sandbox: Name = ""
    version: int
    active_version: int | None = None
    hash: Annotated[str, Field(max_length=128)] = ""
    status: Name = ""  # "effective" for the current revision
    policy_source: Name | None = None
    policy: PolicyBody = PolicyBody()
