#!/usr/bin/env python3
"""Container security audit for the evcrawl stack (AGENTS.md §4).

Looks for the common ways a hosted dev container gets exploited: privilege
escalation inside the container, one member reaching another member or the
host, sibling services (db, redis) accepting more than they should, secrets in
the image or in world-readable files, unpinned supply chain, and known CVEs.

Two modes, chosen automatically:
  host   - run on the machine that runs docker (the host owner). Inspects the
           container from outside (docker inspect / docker exec, read-only) and
           the repository (Dockerfile, compose, CI, hooks).
  inside - run inside the dev container as a member. Only the checks that need
           no docker run; enough for a member's agent to self-check.

Read-only by construction: every probe is `test`, `stat`, `grep`, a SELECT or
a redis read command. Nothing is written except the report files you ask for.

    ops/audit/audit.py --container usher-dev-1 --md docs/security/reports/<date>-usher-dev.md
    ops/audit/audit.py --container usher-dev-1 --cve         # also run trivy (via docker)
    ops/audit/audit.py --inside                               # from a member shell

Standard library only. Exit code: 0 clean, 1 findings at or above --fail-on
(default HIGH), 2 could not run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    category: str
    component: str
    evidence: str
    impact: str
    fix: str
    verify: str
    rebuild: bool = False
    refs: list[str] = field(default_factory=list)
    status: str = "open"


@dataclass
class Check:
    name: str
    result: str  # pass | finding | skipped | error
    detail: str = ""


class Audit:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.container: str | None = args.container
        self.inside: bool = args.inside
        self.repo: str = args.repo
        self.member: str | None = args.member
        self.findings: list[Finding] = []
        self.checks: list[Check] = []
        self.meta: dict[str, str] = {}
        self._n = 0

    # ------------------------------------------------------------ plumbing
    def sh(self, cmd: str, user: str | None = None, timeout: int = 90) -> tuple[int, str]:
        """Run a shell snippet on the host, or inside the container via docker exec."""
        if self.inside or self.container is None:
            argv = ["sh", "-c", cmd]
        else:
            argv = ["docker", "exec"]
            if user:
                argv += ["-u", user]
            argv += [self.container, "sh", "-c", ". /etc/profile.d/evcrawl.sh 2>/dev/null; " + cmd]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            return 127, str(e)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return p.returncode, (p.stdout + p.stderr).strip()

    def host(self, cmd: str, timeout: int = 120) -> tuple[int, str]:
        try:
            p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return p.returncode, (p.stdout + p.stderr).strip()

    def add(self, **kw: object) -> Finding:
        self._n += 1
        f = Finding(id=f"F-{self._n:03d}", **kw)  # type: ignore[arg-type]
        self.findings.append(f)
        return f

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append(Check(name, "pass", detail))

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, "skipped", detail))

    def run_check(self, fn) -> None:  # type: ignore[no-untyped-def]
        before = len(self.findings)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - one broken probe must not stop the audit
            self.checks.append(Check(fn.__name__, "error", f"{type(e).__name__}: {e}"))
            return
        if len(self.findings) > before:
            self.checks.append(
                Check(fn.__name__, "finding", ", ".join(f.id for f in self.findings[before:]))
            )

    def repo_file(self, rel: str) -> str | None:
        p = os.path.join(self.repo, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        return None

    def member_user(self) -> str | None:
        if self.member:
            return self.member
        if self.inside:
            return os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
        txt = self.repo_file("team/members.txt") or ""
        for line in txt.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                self.member = line.split()[0]
                return self.member
        return None

    # ------------------------------------------------------------ host-side checks
    def check_host_config(self) -> None:
        if self.inside or not self.container:
            return self.skip("check_host_config", "needs docker on the host")
        rc, out = self.host(f"docker inspect {shlex.quote(self.container)}")
        if rc != 0:
            raise RuntimeError(out)
        info = json.loads(out)[0]
        hc = info["HostConfig"]
        cfg = info["Config"]
        self.meta["image"] = cfg.get("Image", "")
        self.meta["image_id"] = info.get("Image", "")
        comp = "docker-compose.yml (service dev)"
        if hc.get("Privileged"):
            self.add(
                severity="CRITICAL",
                title="Container runs privileged",
                category="container-escape",
                component=comp,
                evidence="docker inspect: HostConfig.Privileged=true",
                impact="Privileged containers have every capability and raw device access; root inside is root on the host.",
                fix="Remove `privileged: true`; grant the minimal `cap_add` list instead.",
                verify="docker inspect --format '{{.HostConfig.Privileged}}' <container>  -> false",
                rebuild=False,
            )
        else:
            self.ok("privileged", "false")
        bad_caps = {
            "CAP_SYS_ADMIN",
            "CAP_SYS_PTRACE",
            "CAP_SYS_MODULE",
            "CAP_NET_ADMIN",
            "CAP_NET_RAW",
            "CAP_DAC_READ_SEARCH",
            "CAP_SYS_RAWIO",
            "CAP_MKNOD",
        }
        caps = set(hc.get("CapAdd") or [])
        if not (hc.get("CapDrop") and "ALL" in hc["CapDrop"]):
            self.add(
                severity="HIGH",
                title="Capabilities not dropped to a minimal set",
                category="container-escape",
                component=comp,
                evidence=f"CapDrop={hc.get('CapDrop')} CapAdd={sorted(caps)}",
                impact="Default Docker capability set includes NET_RAW, MKNOD and others that widen the attack surface for a root process (sshd) in the container.",
                fix="Set `cap_drop: [ALL]` and add back only what sshd/useradd need (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, SETGID, SETUID, SETPCAP, SYS_CHROOT, AUDIT_WRITE, KILL).",
                verify="docker inspect --format '{{.HostConfig.CapDrop}}' <container>  -> [ALL]",
            )
        elif caps & bad_caps:
            self.add(
                severity="HIGH",
                title="Dangerous capability granted",
                category="container-escape",
                component=comp,
                evidence=f"CapAdd includes {sorted(caps & bad_caps)}",
                impact="These capabilities allow mounting, ptrace of other users, module loading or raw sockets; each is a known escape or lateral-movement primitive.",
                fix="Remove them from `cap_add`.",
                verify="docker inspect --format '{{.HostConfig.CapAdd}}' <container>",
            )
        else:
            self.ok("capabilities", f"cap_drop ALL, cap_add {sorted(caps)}")
        secopt = hc.get("SecurityOpt") or []
        if not any(s.startswith("no-new-privileges") for s in secopt):
            self.add(
                severity="HIGH",
                title="no-new-privileges not set",
                category="privilege-escalation",
                component=comp,
                evidence=f"SecurityOpt={secopt}",
                impact="setuid binaries (su, passwd, mount) can regain root for an unprivileged member.",
                fix="Add `security_opt: [no-new-privileges:true]` to the service.",
                verify="docker inspect --format '{{.HostConfig.SecurityOpt}}' <container>",
            )
        else:
            self.ok("no-new-privileges", str(secopt))
        if any(
            s.startswith("seccomp=unconfined") or s.startswith("apparmor=unconfined")
            for s in secopt
        ):
            self.add(
                severity="HIGH",
                title="seccomp/AppArmor disabled",
                category="container-escape",
                component=comp,
                evidence=f"SecurityOpt={secopt}",
                impact="Removes the default syscall filter that blocks most kernel-level escapes.",
                fix="Remove the `unconfined` entry.",
                verify="docker inspect --format '{{.HostConfig.SecurityOpt}}' <container>",
            )
        for key, val in (
            ("NetworkMode", "host"),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("UTSMode", "host"),
        ):
            if hc.get(key) == val:
                self.add(
                    severity="HIGH",
                    title=f"{key} shares the host namespace",
                    category="container-escape",
                    component=comp,
                    evidence=f"{key}={val}",
                    impact="Host namespace sharing lets container processes see or signal host processes / bind host ports directly.",
                    fix=f"Remove `{key.lower().replace('mode', '_mode')}: host`.",
                    verify=f"docker inspect --format '{{{{.HostConfig.{key}}}}}' <container>",
                )
        binds = list(hc.get("Binds") or [])
        for m in info.get("Mounts") or []:
            src = m.get("Source", "")
            dst = m.get("Destination", "")
            if "docker.sock" in src or "docker.sock" in dst:
                self.add(
                    severity="CRITICAL",
                    title="Docker socket mounted into the container",
                    category="container-escape",
                    component=comp,
                    evidence=f"mount {src} -> {dst}",
                    impact="Anyone who can talk to the socket can start a privileged container and own the host.",
                    fix="Remove the mount. Host-side automation runs on the host.",
                    verify="docker inspect --format '{{json .Mounts}}' <container> | grep -c docker.sock  -> 0",
                )
            elif m.get("Type") == "bind" and re.match(
                r"^/(etc|root|proc|sys|var/run|dev|boot|home/[^/]+/\.ssh)(/|$)", src
            ):
                self.add(
                    severity="HIGH",
                    title="Sensitive host path bind-mounted",
                    category="container-escape",
                    component=comp,
                    evidence=f"bind {src} -> {dst} ({m.get('Mode')})",
                    impact="Members inside the container can read or alter host system files.",
                    fix="Replace the bind with a named volume, or mount read-only a narrower path.",
                    verify="docker inspect --format '{{json .Mounts}}' <container>",
                )
        self.ok("mounts", f"{len(binds)} binds, no docker.sock")
        for port, bindings in (hc.get("PortBindings") or {}).items():
            for b in bindings or []:
                ip = b.get("HostIp") or "0.0.0.0"
                if ip in ("0.0.0.0", "", "::"):
                    self.add(
                        severity="HIGH",
                        title=f"Port {port} published on all host interfaces",
                        category="network-exposure",
                        component=comp,
                        evidence=f"PortBindings[{port}] HostIp={ip!r} HostPort={b.get('HostPort')}",
                        impact="sshd is reachable from every network the host is on, not just the tailnet (AGENTS.md §4 'Access is VPN-only').",
                        fix="Set DEV_SSH_BIND to the Tailscale IPv4 in the host .env; keep the `${DEV_SSH_BIND:-127.0.0.1}` default.",
                        verify="docker port <container>  -> <tailnet-ip>:2222",
                    )
                else:
                    self.ok(f"port {port}", f"bound to {ip}:{b.get('HostPort')}")
        if hc.get("Memory", 0) == 0 or hc.get("PidsLimit") in (None, 0, -1):
            self.add(
                severity="MEDIUM",
                title="No memory or pid cap on the container",
                category="denial-of-service",
                component=comp,
                evidence=f"Memory={hc.get('Memory')} PidsLimit={hc.get('PidsLimit')} NanoCpus={hc.get('NanoCpus')}",
                impact="One runaway agent can exhaust the host.",
                fix="Set `deploy.resources.limits` memory/cpus/pids.",
                verify="docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' <container>",
            )
        else:
            self.ok(
                "resource limits",
                f"mem={hc.get('Memory') // 2**30}g cpus={hc.get('NanoCpus') / 1e9:g} pids={hc.get('PidsLimit')}",
            )
        if hc.get("ReadonlyRootfs"):
            self.ok("read-only rootfs", "true")
        # secrets in the container environment (visible in docker inspect and to root)
        env = cfg.get("Env") or []
        leaked = [
            e.split("=", 1)[0]
            for e in env
            if re.search(r"(PASS|SECRET|TOKEN|KEY|CREDENTIAL)", e.split("=", 1)[0], re.I)
            and not e.startswith(("GPG_KEY=", "PYTHON_SHA256="))
        ]
        if leaked:
            self.add(
                severity="LOW",
                title="Secret-looking variables in the container environment",
                category="secrets",
                component="docker-compose.yml env_file",
                evidence=f"Config.Env names: {leaked}",
                impact="Visible to `docker inspect` on the host and to root inside; only members with root could read them (none today).",
                fix="Pass only the variables the workspace needs with `environment:` instead of `env_file: .env`; keep host-owner-only values out of the container.",
                verify="docker inspect --format '{{json .Config.Env}}' <container>",
            )
        self.ok("container env", f"{len(env)} vars")

    def check_compose_static(self) -> None:
        txt = self.repo_file("docker-compose.yml")
        if txt is None:
            return self.skip("check_compose_static", "no docker-compose.yml")
        if re.search(r"POSTGRES_PASSWORD:\s*(evcrawl|postgres|password|changeme|secret)\b", txt):
            self.add(
                severity="MEDIUM",
                title="Default/hardcoded Postgres superuser password in compose",
                category="weak-credentials",
                component="docker-compose.yml (service db)",
                evidence=re.search(r"POSTGRES_PASSWORD:.*", txt).group(0),  # type: ignore[union-attr]
                impact="The superuser password is public in git; combined with SSH port-forwarding any member can reach the db from their laptop as superuser.",
                fix="Move POSTGRES_PASSWORD to the host-side `.env` (`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}`) and give members a non-superuser role (see the postgres finding).",
                verify="grep -n POSTGRES_PASSWORD docker-compose.yml  -> a ${...} reference",
                rebuild=False,
            )
        if re.search(r"env_file:\s*\.env", txt) and re.search(r"^\s*dev:\s*$", txt, re.M):
            self.ok("env_file", "dev uses env_file (see container env finding if any)")
        if re.search(r"^\s*-\s*\"?/var/run/docker\.sock", txt, re.M):
            self.add(
                severity="CRITICAL",
                title="docker.sock in compose volumes",
                category="container-escape",
                component="docker-compose.yml",
                evidence="volume mapping of /var/run/docker.sock",
                impact="Full host takeover from inside.",
                fix="Remove it.",
                verify="grep -c docker.sock docker-compose.yml -> 0",
            )
        for m in re.finditer(
            r'^\s*-\s*"?(?:\$\{[^}]*:-)?(0\.0\.0\.0|)"?:?(\d+):(\d+)"?', txt, re.M
        ):
            if m.group(1) == "0.0.0.0" or (m.group(1) == "" and "${" not in m.group(0)):
                self.add(
                    severity="HIGH",
                    title="Compose publishes a port on all interfaces",
                    category="network-exposure",
                    component="docker-compose.yml",
                    evidence=m.group(0).strip(),
                    impact="Reachable beyond the tailnet.",
                    fix="Bind to `${DEV_SSH_BIND:-127.0.0.1}`.",
                    verify="docker port <container>",
                )

    def check_dockerfile_pins(self) -> None:
        txt = self.repo_file("Dockerfile")
        if txt is None:
            return self.skip("check_dockerfile_pins", "no Dockerfile")
        unpinned = [
            m.group(0).strip()
            for m in re.finditer(r"^(FROM|COPY --from=)[^\n]*", txt, re.M)
            if "@sha256:" not in m.group(0)
        ]
        unpinned = [u for u in unpinned if not re.search(r"--from=(builder|dev|runtime)\b", u)]
        compose = self.repo_file("docker-compose.yml") or ""
        unpinned += [
            m.group(0).strip()
            for m in re.finditer(r"^\s*image:\s*\S+", compose, re.M)
            if "@sha256:" not in m.group(0)
            and ":local" not in m.group(0)
            and ":dev" not in m.group(0)
        ]
        ci = self.repo_file(".github/workflows/ci.yml") or ""
        actions = [m.group(0) for m in re.finditer(r"uses:\s*\S+@v\d+", ci)]
        if unpinned or actions:
            self.add(
                severity="LOW",
                title="Base images and CI actions pinned by mutable tag, not digest",
                category="supply-chain",
                component="Dockerfile, docker-compose.yml, .github/workflows/ci.yml",
                evidence="\n".join(unpinned + actions),
                impact="A tag can be re-pointed upstream (or by a registry compromise) and the next rebuild silently pulls different bits; builds are not reproducible.",
                fix="Pin `FROM python:3.12-slim@sha256:...`, `COPY --from=ghcr.io/astral-sh/uv:<ver>@sha256:...`, compose `image: postgis/postgis:16-3.5@sha256:...`, and actions by commit SHA (`actions/checkout@<sha> # v4`). Add Renovate or Dependabot (`docker`, `github-actions`) to keep them moving.",
                verify="grep -nE '^(FROM|COPY --from=)|image:|uses:' Dockerfile docker-compose.yml .github/workflows/ci.yml  -> every line has @sha256: or a commit SHA",
                rebuild=True,
            )
        else:
            self.ok("image pins", "all digests")
        if re.search(r"^\s*USER\s+", txt, re.M):
            self.ok("runtime USER", "non-root USER present")
        else:
            self.add(
                severity="HIGH",
                title="Runtime image has no USER directive",
                category="privilege-escalation",
                component="Dockerfile (runtime)",
                evidence="no `USER` line",
                impact="Scheduler runs as root in production.",
                fix="Add a system user and `USER` before ENTRYPOINT.",
                verify="docker inspect --format '{{.Config.User}}' evcrawl:dev",
            )
        for m in re.finditer(r"^\s*(ENV|ARG)\s+\w*(PASS|SECRET|TOKEN)\w*\s*=", txt, re.M | re.I):
            self.add(
                severity="HIGH",
                title="Secret baked into the image via ENV/ARG",
                category="secrets",
                component="Dockerfile",
                evidence=m.group(0),
                impact="Present in every layer and `docker history` forever.",
                fix="Read at run time from the environment.",
                verify="docker history --no-trunc <image> | grep -i secret",
            )

    def check_ci(self) -> None:
        ci = self.repo_file(".github/workflows/ci.yml")
        if ci is None:
            return self.skip("check_ci", "no workflow")
        if "permissions:" not in ci:
            self.add(
                severity="LOW",
                title="Workflow does not restrict GITHUB_TOKEN permissions",
                category="supply-chain",
                component=".github/workflows/ci.yml",
                evidence="no top-level `permissions:` block",
                impact="A compromised action or dependency running in CI gets whatever the repository default token permissions are (often write).",
                fix="Add `permissions: { contents: read }` at the top of the workflow.",
                verify="grep -n 'permissions:' .github/workflows/ci.yml",
            )
        if "trivy" not in ci and "grype" not in ci:
            self.add(
                severity="LOW",
                title="No image vulnerability scan in CI",
                category="supply-chain",
                component=".github/workflows/ci.yml",
                evidence="CI builds dev and runtime images but never scans them",
                impact="Known CVEs in base images or bundled binaries (uv, gosu) ship unnoticed; this audit found several.",
                fix="Add a job using `aquasecurity/trivy-action` on both images with `severity: HIGH,CRITICAL`, `ignore-unfixed: true`, `exit-code: 1` for the runtime image.",
                verify="CI shows a 'scan' job on every PR",
            )

    def check_repo_hygiene(self) -> None:
        gi = self.repo_file(".gitignore") or ""
        di = self.repo_file(".dockerignore") or ""
        missing = [n for n in (".env", ".env.*") if n not in gi]
        if missing:
            self.add(
                severity="HIGH",
                title="Secrets files not gitignored",
                category="secrets",
                component=".gitignore",
                evidence=f"missing: {missing}",
                impact="A `git add -A` commits credentials.",
                fix="Add `.env` and `.env.*` (keep `!.env.example`).",
                verify="git check-ignore .env",
            )
        if ".env" not in di:
            self.add(
                severity="HIGH",
                title=".env not dockerignored",
                category="secrets",
                component=".dockerignore",
                evidence="no .env entry",
                impact="`COPY . .` style steps would bake credentials into the image.",
                fix="Add `.env` and `.env.*`.",
                verify="grep -n env .dockerignore",
            )
        rc, out = self.host(
            f"cd {shlex.quote(self.repo)} && git ls-files | grep -iE '(^|/)(\\.env|id_(rsa|ed25519)|.*\\.(pem|key|p12|pfx))$'"
        )
        if out:
            self.add(
                severity="CRITICAL",
                title="Credential-like file tracked in git",
                category="secrets",
                component=out.splitlines()[0],
                evidence=out,
                impact="Secret is public history.",
                fix="Rotate the secret, remove the file, purge history (`git filter-repo`), force-push with owner approval.",
                verify="git ls-files | grep -E 'pem|key|\\.env$'",
            )
        rc, out = self.host(
            f"cd {shlex.quote(self.repo)} && git log -p --all -- . ':!uv.lock' | grep -iE '^\\+.*(password|secret|token|api[_-]?key)\\s*[=:]\\s*[\"\\x27]?[A-Za-z0-9_./+-]{{8,}}' | grep -viE 'example|evcrawl|\\$\\{{|\\$[A-Z_]+|changeme|placeholder|xxx' | head -5"
        )
        if out:
            self.add(
                severity="HIGH",
                title="Possible secret in git history",
                category="secrets",
                component="git history",
                evidence=out,
                impact="Rotating is the only fix once pushed.",
                fix="Confirm, rotate, purge.",
                verify="re-run this grep",
            )
        else:
            self.ok("git history secrets grep", "nothing matched")
        rc, out = self.host(
            f"stat -c '%a %U %n' {shlex.quote(os.path.join(self.repo, '.env'))} 2>/dev/null"
        )
        if out and not out.startswith("600 ") and not out.startswith("640 "):
            self.add(
                severity="LOW",
                title="Host-side .env is readable by other host users",
                category="secrets",
                component=".env (host)",
                evidence=out,
                impact="Any local account on the host reads the compose credentials.",
                fix="`chmod 600 .env`.",
                verify="stat -c %a .env -> 600",
            )

    def check_guard_hook(self) -> None:
        g = self.repo_file(".claude/hooks/guard.py")
        if g is None:
            return self.skip("check_guard_hook", "no guard hook")
        gaps = []
        for probe, why in (
            ('bash -c "docker ps"', "quoted subshell"),
            ("command sudo id", "`command` prefix"),
            ("env sudo id", "`env` prefix"),
            ('psql -c "select 1; drop table public.events"', "DROP inside a quoted -c string"),
        ):
            payload = json.dumps({"tool_input": {"command": probe}})
            rc, out = self.host(
                f"printf %s {shlex.quote(payload)} | python3 {shlex.quote(os.path.join(self.repo, '.claude/hooks/guard.py'))}"
            )
            if rc == 0:
                gaps.append(f"{probe!r}  ({why})")
        if gaps:
            self.add(
                severity="INFO",
                title="Agent guard hook is bypassable by quoting (guidance, not a boundary)",
                category="agent-guardrails",
                component=".claude/hooks/guard.py",
                evidence="passes through:\n" + "\n".join(gaps),
                impact="None on its own: the real boundaries are the container (no docker, no root). AGENTS.md §4 should not describe the hook as enforcement; the DB rules it guards are only enforced by database privileges (see postgres finding).",
                fix="Word AGENTS.md §4 so 'enforced' covers only the container; optionally extend the regexes to look inside `bash -c`/`sh -c` strings.",
                verify='printf \'%s\' \'{"tool_input":{"command":"bash -c \\"docker ps\\""}}\' | python3 .claude/hooks/guard.py; echo $?',
            )
        else:
            self.ok("guard hook probes", "all blocked")

    # ------------------------------------------------------------ in-container checks
    def check_process_hardening(self) -> None:
        rc, out = self.sh("grep -E '^(NoNewPrivs|Seccomp|CapEff|CapBnd)' /proc/1/status; id -u")
        if rc != 0:
            raise RuntimeError(out)
        if not re.search(r"NoNewPrivs:\s*1", out):
            self.add(
                severity="HIGH",
                title="PID 1 runs without no_new_privs",
                category="privilege-escalation",
                component="docker-compose.yml security_opt",
                evidence=out,
                impact="setuid binaries work for members.",
                fix="`security_opt: [no-new-privileges:true]`.",
                verify="grep NoNewPrivs /proc/1/status -> 1",
            )
        if not re.search(r"Seccomp:\s*2", out):
            self.add(
                severity="HIGH",
                title="No seccomp filter on PID 1",
                category="container-escape",
                component="docker daemon / compose",
                evidence=out,
                impact="Default syscall filter is what blocks most kernel escape primitives.",
                fix="Remove any `seccomp=unconfined`; check `docker info` SecurityOptions.",
                verify="grep Seccomp: /proc/1/status -> 2",
            )
        self.ok("PID 1 hardening", out.replace("\n", "; "))

    def check_suid(self) -> None:
        rc, out = self.sh(
            r"find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -printf '%m %u %p\n' 2>/dev/null | sort -k3"
        )
        files = [line for line in out.splitlines() if line.strip()]
        if files:
            self.add(
                severity="LOW",
                title="setuid/setgid binaries present in the dev image",
                category="privilege-escalation",
                component="Dockerfile (dev stage)",
                evidence="\n".join(files),
                impact="Neutralised today by no-new-privileges, but each is a privilege-escalation primitive if that flag is ever dropped or a kernel bug bypasses it. Key-only sshd needs none of them.",
                fix="In the dev stage after apt-get: `find / -xdev -type f -perm /6000 -exec chmod a-s {} +` (keeps the files, drops the bits). Remove `sudo` if ever installed.",
                verify="find / -xdev -type f -perm /6000 | wc -l  -> 0",
                rebuild=True,
            )
        else:
            self.ok("suid/sgid", "none")

    def check_world_writable(self) -> None:
        rc, out = self.sh(
            r"find / -xdev \( -path /tmp -o -path /proc -o -path /dev -o -path /home \) -prune -o \( -type d -perm -0002 \) -print 2>/dev/null | grep -vE '^/(tmp|var/tmp|dev|proc|run/lock)(/|$)' | cut -d/ -f1-3 | sort -u | head -20"
        )
        dirs = [line for line in out.splitlines() if line.strip() and line != "//"]
        uv = [d for d in dirs if "uv-cache" in d]
        other = [d for d in dirs if "uv-cache" not in d]
        if uv:
            self.add(
                severity="HIGH",
                title="Shared uv cache is world-writable: cross-member code execution",
                category="lateral-movement",
                component="Dockerfile (dev stage): `chmod -R a+rwX /opt/uv-cache`; team/entrypoint.sh UV_CACHE_DIR",
                evidence="drwxrwxrwx root /opt/uv-cache (and every subdirectory / .http metadata file under wheels-v6, sdists-v9, interpreter-v4)",
                impact="uv verifies hashes when it downloads an archive, not when it reuses an already-unpacked wheel from the cache. Any member (or their agent) can edit `/opt/uv-cache/wheels-v6/pypi/<pkg>/...` and the next `uv sync` of every other member links that code into their .venv and runs it under their user. This also contradicts AGENTS.md §4 ('/opt ... read-only for members').",
                fix='1. Dockerfile dev stage: keep the pre-warmed cache but make it read-only: replace `chmod -R a+rwX /opt/uv-cache` with `chmod -R a+rX,go-w /opt/uv-cache`.\n2. team/entrypoint.sh: export `UV_CACHE_DIR=$HOME/.cache/uv` (drop /opt/uv-cache from ENV_VARS; write the per-user value into /etc/profile.d/evcrawl.sh as `export UV_CACHE_DIR="$HOME/.cache/uv"`).\n3. Seed on first login: in the same profile.d snippet, `[ -d "$HOME/.cache/uv" ] || cp -r /opt/uv-cache "$HOME/.cache/uv" 2>/dev/null` so first `uv sync` stays fast.\n4. Mention in AGENTS.md §4 that caches are per user.',
                verify="stat -c %a /opt/uv-cache -> 755; as a member: `test -w /opt/uv-cache; echo $?` -> 1; `uv cache dir` -> /home/<login>/.cache/uv",
                rebuild=True,
                refs=["https://docs.astral.sh/uv/concepts/cache/"],
            )
        if other:
            self.add(
                severity="MEDIUM",
                title="World-writable directories outside /tmp",
                category="privilege-escalation",
                component="Dockerfile",
                evidence="\n".join(other),
                impact="Any member can plant files where another user or a root process may execute or read them.",
                fix="Tighten to 0755 or move under /tmp.",
                verify="find / -xdev -type d -perm -0002 -not -path '/tmp*'",
            )
        if not dirs:
            self.ok("world-writable dirs", "none outside /tmp")

    def check_env_files(self) -> None:
        rc, out = self.sh(
            "ls -l /etc/environment /etc/profile.d/evcrawl.sh 2>/dev/null; grep -oE '^(PGPASSWORD|DATABASE_URL|REDIS_URL)=' /etc/environment 2>/dev/null"
        )
        if "PGPASSWORD" in out or "DATABASE_URL" in out:
            self.add(
                severity="INFO",
                title="Shared database credentials are world-readable inside the container (accepted by design)",
                category="secrets",
                component="team/entrypoint.sh (/etc/environment, /etc/profile.d/evcrawl.sh)",
                evidence=out,
                impact="Every member gets the same db/redis identity; there is no per-member accountability or revocation. AGENTS.md §4 accepts this for a development database. It becomes a real problem only because that identity is a Postgres superuser and redis has no auth (see those findings).",
                fix="No change required once the postgres and redis findings are fixed (the shared credential then names a limited role). If per-member accountability is ever wanted: one Postgres role per login, password generated by the entrypoint into `~/.pgpass` (0600).",
                verify="n/a",
            )
        self.ok("env files", "root-owned 0644, allowlisted variables only")

    def check_users(self) -> None:
        rc, out = self.sh(
            'grep -vE \'(nologin|false|sync)$\' /etc/passwd; ls -l /usr/bin/sudo /usr/bin/doas 2>/dev/null; awk -F: \'$2!="x" && $2!="*" && $2!="!" && $2!~/^!/ {print "unlocked:"$1}\' /etc/shadow'
        )
        if "sudo" in out or "doas" in out:
            self.add(
                severity="HIGH",
                title="sudo/doas installed in the dev image",
                category="privilege-escalation",
                component="Dockerfile",
                evidence=out,
                impact="Any sudoers misconfiguration is root.",
                fix="Remove the package.",
                verify="ls /usr/bin/sudo -> missing",
            )
        if "unlocked:" in out:
            self.add(
                severity="HIGH",
                title="Account with a usable password hash",
                category="weak-credentials",
                component="team/entrypoint.sh",
                evidence=out,
                impact="Password auth is off in sshd, but `su` to that user would work if no_new_privs were ever dropped.",
                fix="`passwd -l <user>` for every account.",
                verify="awk -F: '$2!~/^[!*]/' /etc/shadow -> empty",
            )
        self.ok("users", out.replace("\n", "; ")[:200])

    def check_sshd(self) -> None:
        rc, out = self.sh("/usr/sbin/sshd -T 2>/dev/null")
        if rc != 0 or not out:
            rc, out = self.sh("cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null")
            if not out:
                return self.skip("check_sshd", "sshd not present or not readable (run on host)")
        cfg: dict[str, str] = {}
        for line in out.lower().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                cfg.setdefault(parts[0], parts[1])
        comp = "team/sshd.conf"
        for key, bad, sev, why, fix in (
            (
                "permitrootlogin",
                ("yes", "without-password", "prohibit-password"),
                "CRITICAL",
                "root over SSH",
                "PermitRootLogin no",
            ),
            (
                "passwordauthentication",
                ("yes",),
                "HIGH",
                "password guessing against member accounts",
                "PasswordAuthentication no",
            ),
            (
                "permitemptypasswords",
                ("yes",),
                "CRITICAL",
                "login with empty password",
                "PermitEmptyPasswords no",
            ),
            (
                "x11forwarding",
                ("yes",),
                "LOW",
                "X11 channel to members' desktops",
                "X11Forwarding no",
            ),
            (
                "permittunnel",
                ("yes", "point-to-point", "ethernet"),
                "MEDIUM",
                "layer-3 tunnels from a laptop into the docker network",
                "PermitTunnel no",
            ),
            (
                "permituserenvironment",
                ("yes",),
                "MEDIUM",
                "members override LD_PRELOAD/PATH for their sessions",
                "PermitUserEnvironment no",
            ),
            (
                "gatewayports",
                ("yes", "clientspecified"),
                "MEDIUM",
                "remote forwards bound on all container interfaces",
                "GatewayPorts no",
            ),
        ):
            if cfg.get(key, "") in bad:
                self.add(
                    severity=sev,
                    title=f"sshd: {key} = {cfg[key]}",
                    category="network-exposure",
                    component=comp,
                    evidence=f"sshd -T: {key} {cfg[key]}",
                    impact=why,
                    fix=f"Set `{fix}`.",
                    verify=f"sshd -T | grep {key}",
                )
        if (
            cfg.get("allowtcpforwarding", "yes") in ("yes", "all", "local")
            and cfg.get("permitopen", "any") == "any"
        ):
            self.add(
                severity="MEDIUM",
                title="sshd allows unrestricted TCP forwarding (no PermitOpen)",
                category="lateral-movement",
                component=comp,
                evidence=f"sshd -T: allowtcpforwarding {cfg.get('allowtcpforwarding', 'yes')}; permitopen {cfg.get('permitopen', 'any')}; permitlisten {cfg.get('permitlisten', 'any')}",
                impact="`ssh -L` from any member's laptop reaches anything the container can reach: db:5432 (as superuser today), redis:6379 (no auth today), the docker bridge gateway, and the internet through the host's egress. It also lets laptop-side tools bypass the in-container guard hook entirely.",
                fix="Add to team/sshd.conf: `PermitOpen db:5432 redis:6379` (keeps editor DB tooling working) or `AllowTcpForwarding no` if that is not needed; add `PermitListen none`. Consider `AllowAgentForwarding no` unless members rely on it for git (root in the container could otherwise use their forwarded agent).",
                verify="sshd -T | grep -E 'permitopen|permitlisten|allowtcpforwarding'",
                rebuild=True,
            )
        if cfg.get("authorizedkeysfile", ".ssh/authorized_keys .ssh/authorized_keys2").startswith(
            ".ssh/"
        ):
            m = self.member_user()
            rc2, w = (
                self.sh(
                    "test -w ~/.ssh/authorized_keys && echo writable || echo not-writable", user=m
                )
                if m
                else (1, "")
            )
            if "writable" == w.strip() or m is None:
                self.add(
                    severity="MEDIUM",
                    title="Members can add their own SSH keys (authorized_keys is member-owned)",
                    category="persistence",
                    component="team/sshd.conf (AuthorizedKeysFile), team/entrypoint.sh (install ... $home/.ssh/authorized_keys)",
                    evidence=f"sshd -T: authorizedkeysfile {cfg.get('authorizedkeysfile', '(default)')}; ~{m}/.ssh/authorized_keys owned by {m}, mode 0600, {w.strip()}",
                    impact="AGENTS.md §4 promises 'an agent cannot add a user or a key' and that membership is a commit under team/. In fact any member, or a compromised coding agent running as them, can append a second key and keep access after their key is rotated or removed from team/keys (the entrypoint only deletes the file for logins missing from members.txt; it never rewrites it for active ones).",
                    fix='1. team/sshd.conf: `AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u`.\n2. team/entrypoint.sh: `install -d -m 0755 -o root -g root /etc/ssh/authorized_keys.d` once, then per member `install -m 0644 -o root -g root "$keyfile" /etc/ssh/authorized_keys.d/$login`; delete `/etc/ssh/authorized_keys.d/*` for logins not active. Stop writing `$home/.ssh/authorized_keys`.\n3. Keep `StrictModes yes` (default).',
                    verify="sshd -T | grep authorizedkeysfile -> /etc/ssh/authorized_keys.d/%u; as a member: `echo x >> ~/.ssh/authorized_keys` then a new ssh with a fresh key is refused",
                    rebuild=True,
                )
        if cfg.get("gssapiauthentication", "no") == "yes":
            self.add(
                severity="LOW",
                title="GSSAPIAuthentication enabled",
                category="denial-of-service",
                component=comp,
                evidence="sshd -T: gssapiauthentication yes",
                impact="CVE-2026-60000 (MaxAuthTries mishandled for GSSAPI) needs it on.",
                fix="`GSSAPIAuthentication no`.",
                verify="sshd -T | grep gssapi",
            )
        self.ok("sshd", "root login off, password auth off, key-only, AllowGroups team")

    def check_docker_sock(self) -> None:
        rc, out = self.sh(
            "ls -l /var/run/docker.sock /run/docker.sock 2>/dev/null; command -v docker 2>/dev/null; echo end"
        )
        if "docker.sock" in out or "/docker" in out.replace("docker.sock", ""):
            self.add(
                severity="CRITICAL",
                title="Docker socket or CLI reachable from inside",
                category="container-escape",
                component="docker-compose.yml / Dockerfile",
                evidence=out,
                impact="Host takeover.",
                fix="Remove socket mount and CLI.",
                verify="ls /var/run/docker.sock -> missing",
            )
        else:
            self.ok("docker socket", "not mounted, no CLI")

    def check_limits(self) -> None:
        rc, out = self.sh(
            "p=$(pgrep -u \"$(id -un)\" -x sshd-session 2>/dev/null | head -1); [ -n \"$p\" ] || { echo 'no active ssh session to measure'; exit 0; }; grep -E 'Max (processes|open files|core)' /proc/$p/limits",
            user=self.member_user(),
        )
        if "Max processes" in out and re.search(r"Max processes\s+unlimited", out):
            self.add(
                severity="LOW",
                title="pam_limits not applied to member sessions",
                category="denial-of-service",
                component="team/limits.conf, /etc/pam.d/sshd",
                evidence=out,
                impact="Fork bombs hit the container-wide pid cap, starving other members.",
                fix="Ensure `session required pam_limits.so` in /etc/pam.d/sshd and the limits file is under /etc/security/limits.d.",
                verify="ssh in; ulimit -u -> 256",
            )
        else:
            self.ok(
                "per-user limits",
                out.replace("\n", "; ") or "not measurable outside an ssh session",
            )

    def check_home_quota(self) -> None:
        rc, out = self.sh(
            "df -P /home /tmp 2>/dev/null | tail -n +2; grep -E ' /home ' /proc/mounts"
        )
        if re.search(r"^\S+ /home (btrfs|ext4|xfs|overlay) ", out, re.M) and not re.search(
            r"quota|size=", out
        ):
            self.add(
                severity="MEDIUM",
                title="No size limit on the homes volume: a member can fill the host's root filesystem",
                category="denial-of-service",
                component="docker-compose.yml (volumes: homes), ops/host/README.md",
                evidence=out,
                impact="`/home` is a plain directory on the host's root filesystem (/var/lib/docker/volumes/usher_homes). One dataset download, a runaway agent loop, or a fork of a big repo can fill the disk of the owner's personal computer, taking down the host, the db and every other member. `/tmp` is capped at 4g; `/home` is not. AGENTS.md §4 only asks members to keep homes small.",
                fix="Pick one on the host: (a) btrfs: `btrfs subvolume create /var/lib/docker/volumes/usher_homes/_data` before first `up`, then `btrfs quota enable` + `btrfs qgroup limit 64G` on it; (b) filesystem-agnostic: a fixed-size image `truncate -s 64G homes.img; mkfs.ext4 homes.img` mounted as a loop device and declared as an external volume with `driver_opts: {type: none, o: bind, device: /mnt/usher-homes}`. Document in ops/host/README.md; add a `df` line to the nightly backup script's log.",
                verify="inside: `df -P /home` shows the capped size; host: `btrfs qgroup show` or `losetup -a`",
                rebuild=False,
            )
        else:
            self.ok("home quota", out.replace("\n", "; "))

    def check_redis(self) -> None:
        rc, out = self.sh(
            'command -v redis-cli >/dev/null || exit 3; u="${REDIS_URL:-redis://redis:6379/0}"; redis-cli -u "$u" ACL WHOAMI 2>&1; redis-cli -u "$u" CONFIG GET dir 2>&1 | head -1; redis-cli -u "$u" INFO server 2>&1 | grep -E \'redis_version\'',
            user=self.member_user(),
        )
        if rc == 3:
            return self.skip("check_redis", "no redis-cli")
        if "NOAUTH" in out or "NOPERM" in out or "WRONGPASS" in out:
            self.ok("redis", "auth or ACL restricts CONFIG")
        elif "default" in out and "dir" in out:
            self.add(
                severity="MEDIUM",
                title="Redis accepts unauthenticated admin commands from every member",
                category="lateral-movement",
                component="docker-compose.yml (service redis)",
                evidence=out,
                impact="Any member or agent can `FLUSHALL` the shared queue, `CONFIG SET dir/dbfilename` + `SAVE` to write files inside the redis container (the classic redis-to-shell primitive; the alpine image runs redis as a non-root user, so it is a foothold, not a host escape), and `DEBUG`/`MODULE` if enabled. With unrestricted SSH forwarding this is reachable from laptops too.",
                fix='1. Host `.env`: `REDIS_PASSWORD=<random>`.\n2. compose redis service: `command: ["redis-server", "--requirepass", "${REDIS_PASSWORD:?}", "--rename-command", "CONFIG", "", "--rename-command", "DEBUG", "", "--rename-command", "FLUSHALL", ""]` (or an ACL file with a `team` user `-@dangerous +@all`).\n3. `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0` in the dev and evcrawl services; healthcheck becomes `redis-cli -a "$REDIS_PASSWORD" ping`.',
                verify='redis-cli -u "$REDIS_URL" CONFIG GET dir -> (error) ERR unknown command / NOPERM; PING -> PONG',
                rebuild=False,
                refs=["https://redis.io/docs/latest/operate/oss_and_stack/management/security/"],
            )
        else:
            self.ok("redis", out.replace("\n", "; "))

    def check_postgres(self) -> None:
        rc, out = self.sh(
            "command -v psql >/dev/null || exit 3; psql -Atc \"select current_user||' super='||rolsuper||' createrole='||rolcreaterole||' createdb='||rolcreatedb from pg_roles where rolname=current_user\" 2>&1; psql -Atc 'show server_version' 2>&1; psql -Atc 'show ssl' 2>&1",
            user=self.member_user(),
        )
        if rc == 3:
            return self.skip("check_postgres", "no psql")
        if "super=t" in out:
            self.add(
                severity="HIGH",
                title="Every member connects to Postgres as a superuser",
                category="privilege-escalation",
                component="docker-compose.yml (service db: POSTGRES_USER=evcrawl), team/entrypoint.sh (PGUSER/DATABASE_URL)",
                evidence=out,
                impact="The `evcrawl` role the whole team shares is the cluster superuser. Any member or agent can: run OS commands inside the db container (`COPY ... TO PROGRAM`), read and write files there, read `pg_authid`, create roles, and DROP/TRUNCATE anything regardless of the client-side guard hook (which only inspects shell commands). The db container is on the owner's personal computer. AGENTS.md §4's database discipline is therefore unenforced.",
                fix="1. Keep `evcrawl` (superuser) for `docker compose run --rm evcrawl migrate` only; move its password to the host `.env`.\n2. Add an init script `ops/db/init/10-team-role.sql` mounted at `/docker-entrypoint-initdb.d/` (runs on first init; for the existing volume run it once by hand): `CREATE ROLE team LOGIN PASSWORD '...' NOSUPERUSER NOCREATEROLE NOCREATEDB; GRANT CONNECT ON DATABASE evcrawl TO team; GRANT USAGE, CREATE ON SCHEMA public TO team; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO team; GRANT CREATE ON DATABASE evcrawl TO team;` (CREATE on the database lets members make their `<login>` schema, per §4).\n3. Grant on existing tables once: `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO team; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO team;`.\n4. team/entrypoint.sh: PGUSER/PGPASSWORD/DATABASE_URL default to the `team` role.\n5. Members needing a scratch database (§7) get `CREATEDB` on `team`, not superuser.",
                verify='as a member: psql -Atc "select rolsuper from pg_roles where rolname=current_user" -> f; psql -c "COPY (select 1) TO PROGRAM \'id\'" -> ERROR: permission denied',
                rebuild=True,
            )
        else:
            self.ok("postgres role", out.splitlines()[0] if out else "")
        m = re.search(r"^(1[0-9])\.(\d+)", "\n".join(out.splitlines()[1:2]), re.M)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            latest = {16: 10, 17: 6, 15: 14}.get(
                major
            )  # minors current as of the auditor's knowledge; confirm on postgresql.org
            if latest and minor < latest:
                self.add(
                    severity="HIGH",
                    title=f"PostgreSQL {major}.{minor} is behind the {major}.x security releases",
                    category="known-vulnerabilities",
                    component="docker-compose.yml (image: postgis/postgis:16-3.4)",
                    evidence=f"server_version {major}.{minor}; image postgis/postgis:16-3.4 created 2024-10-14, base Debian 11 (EOSL per trivy)",
                    impact=f"{major}.{minor} predates fixes for CVE-2024-10976/10977/10978/10979 (16.5), CVE-2025-1094 (psql SQL injection, 16.7), CVE-2025-4207 (16.9) and CVE-2025-8714/8715 (16.10) among others; the image's OS layer is end-of-life Debian 11 with 27 CRITICAL unfixed CVEs and PostGIS 3.4.3 has CVE-2026-73515. Reachable by every member (and by forwarded laptops).",
                    fix="Bump `image: postgis/postgis:16-3.5` (exists; same PG major so the data directory is reused, no dump/restore) and pin its digest. After the first start run `ALTER EXTENSION postgis UPDATE;` (or `SELECT postgis_extensions_upgrade();`) once. Add the image to the trivy job. Announce as a rebuild + restart of `db` (AGENTS.md §4). Consider `postgis/postgis:17-3.5` as a separate, planned major upgrade.",
                    verify="psql -Atc 'show server_version' -> 16.10 or later; psql -Atc 'select postgis_full_version()'",
                    rebuild=False,
                    refs=[
                        "https://www.postgresql.org/support/security/",
                        "https://hub.docker.com/r/postgis/postgis/tags",
                    ],
                )
        if "\noff" in out:
            self.ok(
                "postgres ssl",
                "off (docker bridge only; acceptable while traffic never leaves the compose network)",
            )

    # ------------------------------------------------------------ CVE scans
    def check_cve(self) -> None:
        if self.inside or not self.args.cve:
            return self.skip("check_cve", "pass --cve on the host to run trivy via docker")
        images = [self.meta.get("image") or "evcrawl-dev:local", "evcrawl:dev"]
        compose = self.repo_file("docker-compose.yml") or ""
        images += [
            m.group(1)
            for m in re.finditer(r"^\s*image:\s*(\S+)", compose, re.M)
            if m.group(1) not in images and ":local" not in m.group(1) and ":dev" not in m.group(1)
        ]
        outdir = self.args.scan_dir
        os.makedirs(outdir, exist_ok=True)
        for img in images:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", img)
            out_json = os.path.join(outdir, f"trivy-{safe}.json")
            rc, out = self.host(
                f"docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v {shlex.quote(outdir)}:/out -v trivy-cache:/root/.cache/trivy "
                f"aquasec/trivy:latest image --scanners vuln --severity HIGH,CRITICAL --format json -o /out/trivy-{safe}.json {shlex.quote(img)}",
                timeout=900,
            )
            if rc != 0 or not os.path.exists(out_json):
                self.checks.append(Check(f"trivy {img}", "error", out[-300:]))
                continue
            with open(out_json, encoding="utf-8") as fh:
                d = json.load(fh)
            fixable: dict[tuple[str, str, str], list[str]] = {}
            unfixed: dict[str, set[str]] = {}
            crit = 0
            crit_fixable = 0
            for r in d.get("Results") or []:
                for v in r.get("Vulnerabilities") or []:
                    if v["Severity"] == "CRITICAL":
                        crit += 1
                    key = (v["PkgName"], v["InstalledVersion"], v.get("FixedVersion", ""))
                    if v.get("FixedVersion"):
                        if v["Severity"] == "CRITICAL":
                            crit_fixable += 1
                        fixable.setdefault(key, []).append(
                            f"{v['VulnerabilityID']}({v['Severity'][0]})"
                        )
                    else:
                        unfixed.setdefault(v["PkgName"], set()).add(v["VulnerabilityID"])
            os_meta = (d.get("Metadata") or {}).get("OS") or {}
            summary = f"{img}: OS {os_meta.get('Family')} {os_meta.get('Name')}{' (END OF LIFE)' if os_meta.get('EOSL') else ''}; CRITICAL={crit}; fixable packages={len(fixable)}; packages with no fix yet={len(unfixed)}"
            if fixable:
                lines = [
                    f"{p} {iv} -> {fv}: {', '.join(sorted(set(ids))[:5])}{' ...' if len(ids) > 5 else ''}"
                    for (p, iv, fv), ids in sorted(fixable.items())[:15]
                ]
                self.add(
                    severity="HIGH" if crit_fixable or os_meta.get("EOSL") else "MEDIUM",
                    title=f"Fixable HIGH/CRITICAL CVEs in image {img}",
                    category="known-vulnerabilities",
                    component=f"image {img} (Dockerfile / docker-compose.yml)",
                    evidence=summary + "\n" + "\n".join(lines),
                    impact="Each has an upstream fix available; these are the ones a rebuild or image bump actually removes.",
                    fix="Bump the base image / tool version that carries the package (for `uv` bundled crates: bump the uv tag in both COPY --from lines and .mise.toml; for OS packages: `apt-get upgrade` is not the answer, rebuild from a newer base tag). Re-run this audit with --cve.",
                    verify=f"trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 {img}",
                    rebuild=True,
                )
            if unfixed:
                names = sorted(unfixed)
                self.add(
                    severity="LOW",
                    title=f"Unfixed HIGH/CRITICAL CVEs in image {img} (no upstream fix yet)",
                    category="known-vulnerabilities",
                    component=f"image {img}",
                    evidence=summary
                    + "\npackages: "
                    + ", ".join(f"{n}({len(unfixed[n])})" for n in names[:25]),
                    impact="Nothing to rebuild yet; these need tracking so the next rebuild picks up the fix when Debian ships it. The sshd ones matter most for the dev image (see analyst notes).",
                    fix="Add the CI trivy job with `ignore-unfixed: true` so fixable ones fail the build and unfixed ones stay visible; rebuild the dev image at least monthly.",
                    verify="trivy image --severity HIGH,CRITICAL <image> | grep -c 'affected'",
                )
            if not fixable and not unfixed:
                self.ok(f"trivy {img}", "no HIGH/CRITICAL")
            self.meta[f"trivy:{img}"] = summary

    def check_pip_audit(self) -> None:
        if not self.args.cve:
            return self.skip("check_pip_audit", "pass --cve to run pip-audit against uv.lock")
        cmd = (
            "cd ~/usher 2>/dev/null || cd "
            + shlex.quote(self.repo)
            + "; uv export --frozen --format requirements-txt --no-hashes --no-emit-project -o /tmp/audit-req.txt >/dev/null 2>&1 && "
            "uvx pip-audit -r /tmp/audit-req.txt --no-deps --disable-pip --format json 2>/dev/null"
        )
        rc, out = self.sh(cmd, user=self.member_user(), timeout=600)
        try:
            data = json.loads(out[out.index("{") :]) if "{" in out else {}
        except ValueError:
            data = {}
        deps = data.get("dependencies") if isinstance(data, dict) else None
        if deps is None:
            return self.checks.append(Check("pip-audit", "error", out[-300:]))
        vul = [d for d in deps if d.get("vulns")]
        if vul:
            lines = [
                f"{d['name']} {d['version']}: "
                + ", ".join(f"{v['id']} fix {v.get('fix_versions')}" for v in d["vulns"])
                for d in vul
            ]
            self.add(
                severity="HIGH",
                title="Python dependencies with known vulnerabilities",
                category="known-vulnerabilities",
                component="uv.lock",
                evidence="\n".join(lines),
                impact="Runs in both images.",
                fix="`uv lock --upgrade-package <name>` to the fixed version; rebuild.",
                verify="uvx pip-audit -r <(uv export --frozen --no-hashes) --no-deps --disable-pip",
                rebuild=True,
            )
        else:
            self.ok("pip-audit", f"{len(deps)} locked packages, no known vulnerabilities")

    # ------------------------------------------------------------ run + report
    def run(self) -> None:
        self.meta["date"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%MZ")
        self.meta["mode"] = "inside" if self.inside else "host"
        self.meta["target"] = self.container or os.uname().nodename
        rc, sha = self.host(f"git -C {shlex.quote(self.repo)} rev-parse --short HEAD 2>/dev/null")
        self.meta["commit"] = sha if rc == 0 else "unknown"
        if not self.inside:
            for fn in (
                self.check_host_config,
                self.check_compose_static,
                self.check_dockerfile_pins,
                self.check_ci,
                self.check_repo_hygiene,
                self.check_guard_hook,
            ):
                self.run_check(fn)
        for fn in (
            self.check_process_hardening,
            self.check_suid,
            self.check_world_writable,
            self.check_env_files,
            self.check_users,
            self.check_sshd,
            self.check_docker_sock,
            self.check_limits,
            self.check_home_quota,
            self.check_redis,
            self.check_postgres,
        ):
            self.run_check(fn)
        for fn in (self.check_cve, self.check_pip_audit):
            self.run_check(fn)
        self.findings.sort(key=lambda f: (SEVERITIES.index(f.severity), f.id))

    def markdown(self) -> str:
        m = self.meta
        counts = {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES}
        out = [
            f"# Container security audit: {m['target']}",
            "",
            f"- Date: {m['date']}",
            f"- Mode: {m['mode']} (tool: ops/audit/audit.py)",
            f"- Image: {m.get('image', 'n/a')} `{m.get('image_id', '')[:19]}`",
            f"- Repository commit: {m['commit']}",
            "- Findings: " + ", ".join(f"{s.lower()} {counts[s]}" for s in SEVERITIES if counts[s]),
            "",
            "Fix contract: see docs/security/README.md. One PR per finding unless two touch the same file; commit message cites the finding id; set `status` in this file when merged.",
            "",
            "## Summary",
            "",
            "| id | severity | title | component | rebuild | status |",
            "|---|---|---|---|---|---|",
        ]
        for f in self.findings:
            out.append(
                f"| {f.id} | {f.severity} | {f.title} | {f.component.split(',')[0]} | {'yes' if f.rebuild else 'no'} | {f.status} |"
            )
        out += ["", "## Findings", ""]
        for f in self.findings:
            out += [
                f"### {f.id} [{f.severity}] {f.title}",
                "",
                f"- Category: {f.category}",
                f"- Component: {f.component}",
                f"- Rebuild required: {'yes' if f.rebuild else 'no'}",
                f"- Status: {f.status}",
                "",
                "Evidence:",
                "",
                "```",
                f.evidence,
                "```",
                "",
                f"Impact: {f.impact}",
                "",
                "Fix:",
                "",
            ]
            out += f.fix.splitlines()
            out += ["", "Verify:", "", "```", f.verify, "```"]
            if f.refs:
                out += ["", "References: " + ", ".join(f.refs)]
            out.append("")
        out += [
            "## Checks that passed or were skipped",
            "",
            "| check | result | detail |",
            "|---|---|---|",
        ]
        for c in self.checks:
            if c.result != "finding":
                out.append(f"| {c.name} | {c.result} | {c.detail.replace('|', '/')[:160]} |")
        for k, v in m.items():
            if k.startswith("trivy:"):
                out.append(f"| {k} | info | {v} |")
        return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--container", help="container name to audit from the host (docker inspect/exec)"
    )
    ap.add_argument(
        "--inside", action="store_true", help="run inside the container as a member (no docker)"
    )
    ap.add_argument(
        "--member",
        help="member login used for member-perspective probes (default: first in team/members.txt)",
    )
    ap.add_argument(
        "--repo",
        default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        help="repository root (default: this checkout)",
    )
    ap.add_argument(
        "--cve",
        action="store_true",
        help="also run trivy (host, via docker) and pip-audit (via uvx)",
    )
    ap.add_argument(
        "--scan-dir",
        default=os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "tmp", "audit-scan")
        if os.environ.get("CLAUDE_JOB_DIR")
        else "/tmp/audit-scan",
        help="where trivy json goes",
    )
    ap.add_argument("--json", help="write findings as JSON here")
    ap.add_argument("--md", help="write the markdown report here (default: stdout)")
    ap.add_argument(
        "--fail-on",
        default="HIGH",
        choices=SEVERITIES,
        help="exit 1 if any finding is at or above this severity",
    )
    args = ap.parse_args()
    if not args.inside and not args.container:
        if os.path.exists("/.dockerenv") or os.environ.get("EVCRAWL_USER_AGENT"):
            args.inside = True
        else:
            ap.error("pass --container <name> on the host, or --inside within the container")
    a = Audit(args)
    a.run()
    md = a.markdown()
    if args.md:
        os.makedirs(os.path.dirname(os.path.abspath(args.md)), exist_ok=True)
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"report: {args.md}", file=sys.stderr)
    else:
        sys.stdout.write(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "meta": a.meta,
                    "findings": [asdict(f) for f in a.findings],
                    "checks": [asdict(c) for c in a.checks],
                },
                fh,
                indent=2,
            )
    worst = min((SEVERITIES.index(f.severity) for f in a.findings), default=len(SEVERITIES))
    return 1 if worst <= SEVERITIES.index(args.fail_on) else 0


if __name__ == "__main__":
    sys.exit(main())
