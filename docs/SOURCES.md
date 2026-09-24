# Findings: capacity and cost patterns for CI at agentic-coding scale

A sourced research note supporting the patterns in this repository. Every claim
below is attributed, and the provenance of each claim is marked explicitly.
Primary sources are linked; where a statement is our own design decision rather
than something read from a source, it says so.

**Provenance markers used throughout**

| Marker | Meaning |
| --- | --- |
| **[SOURCE]** | Stated in a primary source. Quoted or closely paraphrased. |
| **[OBSERVED]** | A fact about a particular environment that we verified directly. Scoped to that environment only; not a general claim. |
| **[UNVERIFIED]** | Asserted elsewhere but *not* verified by us. Not relied upon. |
| **[OURS]** | Our own design decision, safeguard, or interpretation. Not from any source. |

This repository contains no secrets, no hostnames, no private repository
references, and no token values. Anything environment-specific is described in
generic terms.

---

## 1. The growth problem

**[SOURCE]** Anthropic's test-impact-analysis write-up reports that CI job
volume grew **25x over six months**, the number of tests across their codebase
grew **10x**, and engineering headcount grew only nominally. The same post notes
that Claude authors **80%** of the code written there and that engineers ship
roughly **8x** as much code per quarter as they did from 2021–2025.

Source: *Agentic coding is straining CI. Here's how we scaled test impact
analysis at Anthropic*, Sachin Malhotra, September 14 2026 —
<https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic>

**[SOURCE]** The same source states its central lesson in its own words: the
specific scaling techniques — bigger machines, parallelising processes,
restarting the service — are "common and **not the insights to take from this
article**." The insight offered instead is that "each of these techniques bought
a fraction of the time they did a year ago," and that overhauling the service
"also takes a fraction of the time" now that writing code is not the bottleneck.

**[SOURCE]** The planning guidance is explicit: assume your architecture "will be
at a 25x load within two quarters," and it is now reasonable to design for
"10-20x the perceived scale" in a v0 where budget allows. The source also
predicts that "horizontally scaled test selection architecture will become
industry standard."

### 1.1 The listener/selector split, in the source's own terms

**[SOURCE]** The service "depends on two deterministic components staying in
sync":

- **A "listener"** records the test results from every CI run.
- **A "selector"** reads the test result history and determines which tests run
  on which opened PRs.

**[SOURCE]** The failure mode is stated precisely: with multiple CI jobs running
every second, "the listener starts to increasingly fall behind the PR queue."
Twenty minutes of listener lag "can translate into tens of thousands of test
updates not being applied to the selector." The consequences named are that a
bad change merged causes a test to fail for everyone; a flaky dependency starts
blocking merges; and a fixed or newly added test will not run until the listener
catches up.

**[SOURCE]** The reason the original design could not scale horizontally is
architectural: it "ran as a single process because keeping a running history per
test meant a single writer needed to apply the results."

### 1.2 The three patches, and how long each lasted

**[SOURCE]** The source gives durations, which are the most useful numbers in
the piece because they quantify how quickly a patch is consumed by exponential
growth:

| Patch | What it was | How long it bought |
| --- | --- | --- |
| 1 | Doubling the cores running the service | 70 days |
| 2 | Splitting each package's state into a shard with its own worker | 29 days |
| 3 | Daily restarts to work around a memory limit | "less than a day" |

**[SOURCE]** On patch 3, the source records a consequence that matters for any
selector: daily restarts caused the service to fall further behind, and when it
fell behind by more than an hour, "a ton of job results weren't recorded by the
listener." The source is careful about scope here — this did *not* mean untested
code reached production. What it meant was that "our test-selection component
was using stale data to decide what to run and what not to on PRs."

That distinction is the reason this repository treats staleness as a
first-class, reportable state rather than an error to be swallowed.

### 1.3 The redesign

**[SOURCE]** The redesign gave the service "a database, or an in-memory data
store to be exact," offloading in-memory processing from the singleton. In the
new shape, "any listener worker can process any result, append it to a journal
in the in-memory store, and move on without holding anything in memory —
stateless and hence, horizontally scalable. A small separate consumer process
rolls the journal up into per-test history every few seconds."

**[SOURCE]** The trade is stated honestly: "This distributed architecture is
more expensive to run, but it is much easier to scale and memory profile than a
shaky singleton." The project took three weeks for a single engineer.

**[SOURCE]** The two closing recommendations relevant here: instrument so that
"the same number of CI jobs coming in equals the same going out," and "keep
state out of the process from the start," avoiding a single instance for any
critical service unless it is measurable and canary-able.

### 1.4 What this repository adds, and what it does not claim

**[OURS]** The source describes a real production system and its operational
history. It does **not** publish a test-selection algorithm, a dependency-graph
format, or a staleness policy. The selector in `examples/selector_demo.py` is
therefore *not* a reimplementation of it and makes no claim to be. What is
borrowed is the architectural distinction — listener versus selector, and the
fact that staleness degrades selection quality silently — and the specific
fail-closed safeguards are ours:

- An unmapped changed file selects the **full** suite. The source does not
  prescribe this; we chose it because the alternative is a green build that ran
  nothing.
- History older than a freshness window selects a **full run** for that suite.
  The source describes stale data being used to decide; we chose to make
  staleness a conservative widening rather than a silent narrowing.
- A history payload that cannot be parsed is an **error**, not an empty history.
  The source notes results going unrecorded during an outage; we chose to treat
  an unreadable record as a fault rather than as "no known failures."

**[OURS]** The queueing model in `examples/cisim/queue.py` is a small teaching
model, not a reproduction of the source's load. Its scenarios are illustrative
configurations; the numbers are a shape, not a forecast. The one property the
model enforces absolutely is that completed work can never exceed accepted work.

---

## 2. Runner capacity: hosted, self-hosted, ephemeral

### 2.1 CircleCI's self-hosted runner

**[SOURCE]** CircleCI documents three self-hosted installation shapes:

- **Container runner** — installed in a Kubernetes cluster. It "claim[s] your
  containerized jobs, schedule[s] them within an ephemeral pod, and execute[s]
  the work within a container-based execution environment." Pods "are torn down
  after the jobs have completed."
- **Machine runner** — installed in a VM or natively on a physical machine.
  Each job "executes in the same environment (virtual or physical) where the
  self-hosted runner binary is installed." Not compatible with CircleCI
  convenience images or custom Docker images.
- **Machine Runner Orchestrator** — a Kubernetes controller that "automatically
  scales CircleCI runner VMs using KubeVirt," where "each job runs inside a full
  virtual machine that is provisioned on demand and shut down after the job
  completes." It "polls the CircleCI API for pending and running tasks, then
  adjusts a `VirtualMachinePool` replica count to match demand," with a
  configurable `minReplicas` that "keeps a pre-warmed pool of VMs ready to claim
  jobs immediately." CircleCI describes the isolation as stronger than
  container runner.

Source: *CircleCI's self-hosted runner overview* —
<https://circleci.com/docs/guides/execution-runner/runner-overview/>

**[SOURCE]** CircleCI also records that Launch agent 1.1 is deprecated, with
Machine Runner 3.0 as the recommended replacement, and that a couple of standard
features are unavailable on runner executors (Docker layer caching; some
deprecated cloud environment variables).

### 2.2 Namespaces, resource classes, and the routing contract

**[SOURCE]** A self-hosted runner requires both a **namespace** — "a unique
identifier claimed by a CircleCI organization," one per organization, immutable
— and a **resource class**, defined as "a label to match your CircleCI job with
a type of runner that is identified to process that job." The first part of the
resource class is the organization's namespace; `circleci/documentation` is the
documented example. Resource classes are created during runner installation, and
the documented use is to distinguish pools such as `orgname/macOS` and
`orgname/linux`.

Source: *Self-hosted runner concepts* —
<https://circleci.com/docs/guides/execution-runner/runner-concepts/>

**[SOURCE]** On the job side, the mechanism is the `resource_class` key. The
same key selects hosted ARM execution (`arm.medium` or `arm.large` on a `machine`
executor) and selects a self-hosted pool
(`<my-namespace>/<my-runner>`). CircleCI notes that if no resource class is
declared a default is used, and that defaults "are subject to change," so
specifying one explicitly is best practice.

Source: *Resource class overview* —
<https://circleci.com/docs/guides/execution-managed/resource-class-overview/>

### 2.3 GitHub's self-hosted runner

**[SOURCE]** GitHub documents the routing algorithm precisely, and the two
timeouts are the operationally important part: GitHub looks for a runner
matching the job's `runs-on` labels and groups; an **online and idle** match is
assigned the job; if the runner "doesn't pick up the assigned job within 60
seconds, the job is re-queued so that a new runner can accept it"; if no match
exists the job "will remain queued until a runner comes online"; and if it
"remains queued for more than 24 hours, the job will fail."

Source: *Self-hosted runners reference* —
<https://docs.github.com/en/actions/reference/runners/self-hosted-runners>

**[SOURCE]** The same reference states GitHub's position on autoscaling
persistent runners: "GitHub recommends implementing autoscaling with ephemeral
self-hosted runners; autoscaling with persistent self-hosted runners is not
recommended." The stated reason is that "in certain cases, GitHub cannot
guarantee that jobs are not assigned to persistent runners while they are shut
down," whereas with ephemeral runners "GitHub only assigns one job to a runner."
Ephemeral runners are registered with the `--ephemeral` flag, and GitHub notes
the runner application log files "must be forwarded to an external log storage
solution" because the runner is destroyed.

**[SOURCE]** For Kubernetes, Actions Runner Controller (ARC) is described as "the
reference implementation of GitHub's scale set APIs and the recommended
Kubernetes-based solution for autoscaling self-hosted runners." The Runner Scale
Set Client is offered as a complementary tool for building custom autoscaling
*outside* Kubernetes, explicitly "not a replacement for ARC." A third option,
reacting to the `workflow_job` webhook, is documented with a caveat: it "relies
on the timeliness of webhook delivery for making scaling decisions, which can
introduce delays and reliability concerns."

**[SOURCE]** Runner software updates carry an operational deadline: if automatic
updates are disabled, the runner version "must" be updated within 30 days of a
release, and GitHub states that it "will not queue jobs to your runner" if that
window is missed, or immediately for a critical security update.

**[SOURCE]** On the public-repository question, GitHub's runner documentation
recommends "that you only use self-hosted runners with private repositories.
This is because forks of your repository can potentially run dangerous code on
your self-hosted runner machine by creating a pull request that executes the
code in a workflow." This text is quoted in the related-sciences project's
README (see §4) and is consistent with GitHub's own guidance.

### 2.4 CircleCI on public repositories and fork pull requests

**[SOURCE]** CircleCI is unambiguous, and this is the strongest single citation
for the rule this repository follows. Self-hosted runners are **"Not Available"**
for use with public projects that have the *Build forked pull requests* setting
enabled. The documentation states this is "not available for security reasons,"
and enumerates the risks: malicious programs running on the machine, escaping
the runner sandbox, exposing access to the machine's network environment, and
"persisting unwanted or dangerous data on the machine," with the note that this
is especially acute "if your machine persists its environment between jobs."

Source: *Self-hosted runner concepts* (Public repositories section) —
<https://circleci.com/docs/guides/execution-runner/runner-concepts/>

**[SOURCE]** CircleCI also limits organizations to claiming one namespace by
default, "to limit name-squatting and namespace noise."

### 2.5 Cache is a trust boundary

**[SOURCE]** GitHub documents cache scope and the cache-poisoning class of
attack. A workflow run can restore caches created in the current branch or the
default branch, and a pull-request run can additionally restore caches from the
base branch — including base branches of forked repositories. Runs cannot
restore caches from child or sibling branches.

**[SOURCE]** On which triggers may write to the default branch's cache scope,
GitHub restricts creation/overwrite to `push`, `workflow_dispatch`,
`repository_dispatch`, `delete`, `registry_package`, `page_build`, and
`schedule`. Runs triggered by any other event resolving to the default branch
get **read-only** cache access — "this includes triggers whose payload or
initiating actor can be influenced by someone outside the repository, such as
`pull_request_target`, `issue_comment`, and `workflow_run`." The attack is named
directly: "This class of attack is known as *cache poisoning*."

**[SOURCE]** The `cache-mode` key (`read`, `write`, `write-only`, `none`)
control cache access, with `cache-mode` omitted defaulting to `write` for
trusted triggers and `read` for low-trust triggers. GitHub warns that explicitly
declaring `cache-mode: write` on a low-trust trigger "reintroduces the risk of
cache-poisoning that the default untrusted-trigger read-only permissions are
designed to prevent."

Source: *Dependency caching reference* —
<https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching>

### 2.6 Comparison of capacity approaches

| Approach | Isolation model | Cost shape | Principal drawback |
| --- | --- | --- | --- |
| Provider-hosted per-minute | Provider-managed | Per minute, scales with job count | Cost tracks the axis that just went exponential |
| Persistent self-hosted | Shared machine, reused across jobs | Lowest per minute | GitHub: jobs may be assigned to runners while shutting down; state persists between jobs |
| Ephemeral self-hosted | One job per runner, destroyed after | Ownership of provisioning and drain | Cold-start latency; logs must be shipped externally |
| Container on Kubernetes | Ephemeral pod per job | Cluster-elastic | Requires a cluster and operators |
| VM on Kubernetes (KubeVirt) | Full VM per job | Cluster-elastic, pre-warm costs | Slowest cold path; heaviest operator burden |

**[OURS]** The comparison and its framing are ours; each row's properties are
drawn from the sources above. The `examples/fixtures/pools.json` fixture encodes
one pool per row so the lifetime simulator exercises each kind.

---

## 3. What we verified ourselves, and what we did not

**[OBSERVED, 2026-09-24]** In a separate private project, a CircleCI-hosted
`arm.medium` ARM64 job checked out an issue branch and ran a bounded subset of
the project's canonical CI entrypoint. The job reported success with 125 tests
passed. This proves that hosted ARM64 path for that subset, not its full merge
gate. The project initially refused a pipeline because its OAuth checkout key
was absent; a read-only deploy key resolved that prerequisite. No self-hosted
CircleCI runner was installed or observed.

**[UNVERIFIED]** Whether the same pipeline passes the other mandatory suites,
reduces end-to-end queueing, or can safely replace existing required checks
has not been established. A CircleCI account, an OAuth project, a checkout key,
and a hosted job are distinct from a registered self-hosted runner and from
any external node's admission. Hosted minutes may also incur provider charges.

**[OURS]** The examples *in this public repository* remain offline simulations;
their capacity numbers are model output, not field telemetry. A provider-hosted
pilot is an observed option, not a deployed replacement for the existing CI
control plane.

### 3.1 Bounded engineering-activity snapshot

**[OBSERVED, through 2026-09-24T08:43:24Z]** An authorized read of two
access-controlled engineering projects counted GitHub Actions **workflow runs**
by `created_at` and pull requests **opened** by creation date for UTC weeks
starting 2026-08-03 through the partial week ending 2026-09-24. The generic
categories are *fabric implementation* and *data platform*. The totals in this
window are 329 and 1,659 workflow runs respectively, and 46 and 177 opened
PRs. These are two distinct project populations, not a combined throughput
benchmark. The [aggregate snapshot](diagrams/activity-snapshot.json) records
the eight weekly pairs and conclusion counts; the [figure](diagrams/activity-weekly.svg)
plots those same counts. Run totals include success, failure, cancellation, and
**504 skipped runs** across both projects. A workflow run is not a job, minute,
queued task, or measure of engineering headcount. PR creation is not a merge.
The last week covers only Sep 21–24 to the capture instant; earlier weeks are
seven-day UTC windows. CircleCI is **not** counted in this series.

**[OURS]** Reproduce the SVG offline with `python3 examples/activity_figure.py`;
an authorized operator may refresh the frozen source once with
`python3 examples/activity_figure.py --collect OWNER/FABRIC_REPO
OWNER/DATA_REPO --cutoff 2026-09-24T08:43:24Z` (substitute access-controlled
repository paths locally; do not publish them). Collection reads GitHub REST
`repos/{owner}/{repo}/actions/runs?per_page=100&page=N` through the lower
date bound and `search/issues?q=repo:{owner}/{repo} type:pr created:START..END`
for each week. It stores only aggregate categories, UTC dates, counts, and
conclusions. Source availability and historical retention can change, so
recollection at a later date is not guaranteed to yield the same results;
the checked-in aggregate is the frozen evidence. Neither this count nor the
historical controller durations in the README establish a queue improvement.

### 3.2 Candidate control-plane and external-compute boundary

**[OURS, proposed; not deployed by this repository]** A Cloudflare Worker and
Queue could own bounded job identity and state, and dispatch only after a Role,
data-class, and cost gate to an admitted Node Slot. A separate, **unadmitted**
Modal provider adapter is a candidate for one public/synthetic burst CPU/GPU
pilot; it would not inherit Node Slot admission. Completion must return by an
authenticated, idempotent callback. A current CI path uses a thin controller
and an ephemeral Novita sandbox, but its synchronous controller occupancy is
not repaired merely by drawing the candidate path. This public-safe summary
was derived from the access-controlled research note at commit
`a4ad9e87621c51fa0dd4723c00bdb93d20a56cc0` (2026-09-24); the private
repository location is deliberately not published. Neither the note nor this
diagram authorizes purchase, deployment, data migration, or provider admission.
Public fabric concepts and the proposed Cloudflare scope are in the [fabric architecture](https://github.com/Kitkitkittt/heterogeneous-compute-fabric/blob/main/docs/architecture.md)
and [design issue](https://github.com/Kitkitkittt/heterogeneous-compute-fabric/issues/41).

**[SOURCE, checked 2026-09-24]** Product and pricing boundaries for this
candidate: [Cloudflare Workers](https://developers.cloudflare.com/workers/platform/pricing/),
[Queues](https://developers.cloudflare.com/queues/platform/pricing/), and
[Modal pricing](https://modal.com/pricing). Verify prices and limits again before
implementation. None of those provider references proves that the candidate
route is installed, cheaper, faster, or authorized for non-public data.

---

## 4. Architecture references

**[SOURCE]** `related-sciences/gce-github-runner` (Apache-2.0) — cited as an
**architecture reference only**, for one pattern: a setup job that creates an
ephemeral VM, registers a runner with a unique label, exposes that label as a
job output, and lets a downstream job select it via
`runs-on: ${{ needs.create-runner.outputs.label }}`. The project's own README
notes the VM "will be automatically shut down after the workflow" via the
self-hosted runner post-job hook, and lists the runner image requirements
(`gcloud`, `git`, optionally a preinstalled Actions runner).

Source: <https://github.com/related-sciences/gce-github-runner>

**[OURS]** No code from that project is copied into this repository. It is not
affiliated with this work, and no part of it is reproduced here. It is listed as
a citation for the ephemeral-runner-plus-generated-label pattern, which is one
of several ways to solve the routing problem the lifetime simulator models.

**[OURS]** Other approaches to the same problem, referenced for comparison
rather than reproduced: Actions Runner Controller and the Runner Scale Set Client
(§2.3), and CircleCI's Machine Runner Orchestrator (§2.1). The choice between
them is a choice about *where your trust boundary sits*, not primarily about
throughput.

---

## 5. Design rules this repository derives

Derived from the sources above; the reasoning is ours, the constraints are
theirs. The README applies these in its deploy-safety section.

1. **Never attach self-hosted capacity to untrusted fork pull requests.** Both
   CircleCI (hard unavailability for public projects with fork builds enabled)
   and GitHub (recommendation to use self-hosted runners only with private
   repositories) support this. Consequence for a public repository: no
   self-hosted-runner workflow may exist in it.
2. **Enforce trust domains with access controls, not labels.** GitHub runner
   [groups](https://docs.github.com/en/actions/concepts/runners/runner-groups)
   constrain repository access (where available); workflow admission controls
   which code executes. [Labels select runners](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job),
   but are not an authorization boundary. CircleCI resource classes also route
   jobs; configure access and fork admission separately (§2.2, §2.3).
3. **Prefer ephemeral over persistent** for anything autoscaled (§2.3).
4. **Ship runner logs off the runner** if the runner is ephemeral (§2.3).
5. **Treat cache as a trust boundary**, and do not grant write-capable cache
   access on low-trust triggers (§2.5).
6. **Make staleness visible.** A selector running on stale history is not
   failing loudly; it is quietly choosing the wrong tests (§1.2). Fail closed
   and report degradation.
7. **Pick a label strategy before sizing compute.** Mismatched labels present as
   jobs queued until a timeout, not as errors (§2.3).

---

## 6. Full source list

| Source | What it supports |
| --- | --- |
| Anthropic, *Agentic coding is straining CI* (2026-09-14) | 25x/10x growth; listener/selector split; three patches and durations; journal redesign; 25x-in-two-quarters guidance |
| CircleCI, *Self-hosted runner overview* | Container runner, machine runner, Machine Runner Orchestrator; ephemeral pods; VM lifecycle; `minReplicas` pre-warm |
| CircleCI, *Self-hosted runner concepts* | Namespaces; resource classes as labels; task-agent/container-agent; public-repository fork restriction and its stated risks |
| CircleCI, *Resource class overview* | `resource_class` key; hosted ARM classes; self-hosted pool selection; default-when-omitted caveat |
| GitHub, *Self-hosted runners reference* | Routing precedence; 60-second re-queue; 24-hour queue timeout; ephemeral-runner recommendation; ARC; webhook autoscaling caveat; 30-day update deadline |
| GitHub, *Self-hosted runners* (concepts) | Recommendation to use self-hosted runners with private repositories only |
| GitHub, *Dependency caching reference* | Cache scope; low-trust trigger restrictions; cache poisoning; `cache-mode` |
| `related-sciences/gce-github-runner` (Apache-2.0) | Ephemeral VM + generated label pattern. Architecture citation only; no code copied |
