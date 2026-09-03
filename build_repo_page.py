"""
Build rtt-repo-spec.html from the actual files in rtt-pipeline/.

The page is generated from the source so the two cannot drift. Each module's
docstring becomes the prose; the rest of the file becomes the code block.
"""
import ast
import html
import re
from pathlib import Path

REPO = Path("rtt-pipeline")
OUT = Path("rtt-repo-spec.html")
TEMPLATE = Path("rtt-repo-spec.html")     # reuse the existing head/CSS

FILES = [
    # (path, rail note, heading, extra notes [(kind, tag, html)], must-not)
    ("rules/nhs_number.py", "the easy one",
     "Modulus 11, as an expression",
     [("eli5", "ELI5",
       "A valid NHS number carries its own receipt. The last digit is arithmetic "
       "on the first nine, so a typo almost always fails to add up.")],
     "Be a Python function called row by row. At 1.2M referrals that is minutes of "
     "interpreter overhead to answer a question arithmetic answers in one pass."),

    ("rules/clocks.py", "the crux",
     "Splitting an event stream into clocks, and judging a DNA",
     [("trap", "The two checks worth making by hand",
       "<p><strong>No literal code sets.</strong> <code>start_codes</code> and "
       "<code>stop_codes</code> are parameters. A module holding "
       "<code>START_CODES = {\"10\",\"11\",\"12\"}</code> is exactly what the "
       "<code>ref_rtt_status</code> feed exists to prevent.</p>"
       "<p><strong><code>min()</code>, not <code>first()</code>.</strong> "
       "<code>group_by</code> makes no promise about order. <code>first()</code> "
       "passes on the seed cohort and starts lying at volume tier, which is the "
       "worst possible failure schedule.</p>"),
      ("", "Why the verdict comes back as a string",
       "<p><code>resolve_dna_stops</code> returns <code>dna_verdict</code> rather "
       "than raising anything. Rules judge; transforms route. That is what lets "
       "the same function be tested with four rows and no notion of what a "
       "Validation Task is.</p>")],
     "Decide what happens to a misrecorded stop. It reports the verdict; "
     "<code>pathway.py</code> decides who gets told."),

    ("rules/measures.py", "the design argument",
     "Invariants, and measures as at a date",
     [("trap", "Two questions, opposite answers",
       "<p><strong>\"How long had this person waited as at 31 March\"</strong> is a "
       "statutory return and must be frozen. Recompute it later and a published "
       "figure stops being reproducible.</p>"
       "<p><strong>\"How long has this person been waiting right now\"</strong> - a "
       "validator opening the list on a Tuesday in June - must be live. Freeze it "
       "and every open pathway reports the wait it had at the last build.</p>"
       "<p>So the <code>pathway</code> dataset carries neither. It carries the "
       "breach <em>dates</em>, which do not move with the calendar, and "
       "<code>ptl_snapshot</code> carries the frozen count.</p>"),
      ("", "Why a date and not a count",
       "<p>A week count is a function of today, and today is not input data - no "
       "incremental run will revisit it, and a nightly build only makes it a day "
       "fresh. A breach date is a function of <code>clock_start_date</code> alone. "
       "\"Who is breaching\" becomes <code>breach_18wk_date &lt;= today</code>: an "
       "indexed date comparison that filters and aggregates, with no read-time "
       "arithmetic for a derived property or a function-backed column to do.</p>"),
      ("trap", "The flattering error",
       "<p>A nullified pathway has <code>None</code> weeks, not <code>0</code>. "
       "Zero weeks is comfortably within 18, so it lifts the compliance figure. "
       "Every error in this file moves the headline number, and the ones that move "
       "it upwards are the ones nobody reports.</p>")],
     "Put <code>weeks_waiting</code> on the Pathway object. Correct "
     "<code>clock_start_date</code> through an Action and the stale count sits "
     "beside it, unchanged and unchallenged."),

    ("rules/providers.py", "GIVEN",
     "Succession, and resolving a code as at a date",
     [("", "Given, and why",
       "<p>This ships in the starter repository. Inferring succession from ODS "
       "adjacency is fiddly and teaches little. What is assessed is that trainees "
       "call <code>resolve_as_at</code> with the RIGHT DATE at each of its two "
       "call sites &mdash; referral date in <code>referral.py</code>, snapshot date "
       "in <code>publish.py</code>. Get that wrong and you publish a row reading "
       "\"RZB / Eastvale and Northmoor\": a code and a name that never coexisted on "
       "any day.</p>")],
     "Resolve to \"now\". Every caller has a date in mind, and they are different "
     "dates: the referral row wants the code as at referral, the PTL wants it as "
     "at the snapshot."),

    ("transforms/reference.py", "GIVEN",
     "Three reference feeds, and where the code sets come from",
     [("", "Given, and why",
       "<p>Typing three reference feeds teaches nothing the rest of Project 1 does "
       "not teach better, and two days is tight. Trainees still read it, because "
       "this is where <code>START_CODES</code> and <code>STOP_CODES</code> come "
       "from and that is load-bearing from step 5 onwards.</p>")],
     "Decide anything. Note it is also the file that makes "
     "<code>rules/</code>'s no-literals rule possible."),

    ("transforms/referral.py", "wiring + one rule",
     "Deduplicate, validate, and emit findings",
     [("trap", "The habit to break",
       "<p>The reference solution accumulates exceptions in a Python list because "
       "it runs in one process. A pipeline cannot: each transform is its own job. "
       "Every transform that notices something writes a findings dataset, and "
       "<code>publish.py</code> unions them. A module-level list here is the "
       "single most common thing a notebook habit produces, and it works right up "
       "until the transform runs on more than one node.</p>"),
      ("", "The finding is the mapping",
       "<p>A collapsed duplicate's finding carries both identifiers - "
       "<code>record_id</code> is the referral that lost, <code>referral_id</code> "
       "is the one that survived - so <code>activity.py</code> reads the dedup map "
       "straight out of it. No second source of truth, no state shared between "
       "jobs. Your build log is data.</p>")],
     "Delete a row because its NHS number is wrong. The identifier is wrong; the "
     "patient is not. That single decision is the difference between 38.5% and "
     "the 36.4% diagnostic wrong answer."),

    ("transforms/activity.py", "the long one",
     "Three feeds, three shapes, one event stream",
     [("trap", "Same referral, opposite treatment",
       "<p>A collapsed duplicate carries both status events and real care "
       "activity. Discard the status events - reparenting them opens a second "
       "clock and re-inflates the waiting list, which is what the deduplication "
       "was for. Reparent the care activity - it happened to a person, and it is "
       "the evidence that decides whether a later DNA nullifies.</p>"
       "<p>A trainee who treats both the same way has not noticed that a status "
       "code is a claim about the clock and an attendance is a claim about the "
       "world.</p>")],
     "Drop an event it cannot place. An emergency admission with no referral is "
     "kept and attached to nothing; an orphaned referral becomes a HIGH finding. "
     "Nobody counts the rows that were never there."),

    ("transforms/pathway.py", "wiring, ordered",
     "Clocks, then invariants",
     [("trap", "Read the order twice",
       "<p>Events are attached to clocks <em>before</em> the DNA question is "
       "asked, because \"was this the first care activity on this clock\" cannot "
       "be answered until you know which clock the activity is on. Then, after a "
       "misrecorded stop is cleared, the events are re-stamped - the window moved, "
       "so the attachment has to move with it. Skip the second pass and an event "
       "sits on a pathway whose window no longer contains it.</p>")],
     "Contain an RTT rule. Read it top to bottom: it hands frames to "
     "<code>rules.clocks</code> and <code>rules.measures</code> and routes what "
     "comes back."),

    ("transforms/publish.py", "the split",
     "The frozen PTL, and work versus housekeeping",
     [("trap", "Not everything your pipeline notices is somebody's job",
       "<p>Six Validation Tasks against three build log entries on the seed "
       "cohort; 3,601 against 3,706 on the full tier. Route them to one queue and "
       "a validator opens their morning worklist to find 51% of it is noise - and "
       "then stops reading it. The split is the teaching point of the whole "
       "project.</p>"),
      ("", "Deterministic identifiers",
       "<p>Tasks are numbered by <code>(layer, record_id)</code>, not by arrival. "
       "Arrival order is whatever the executor felt like today, and a "
       "<code>task_id</code> that changes between builds of the same data is a "
       "<code>task_id</code> nobody can cite in a ticket.</p>")],
     "Write anything into a task beyond <code>OPEN</code>. Status, assignee, "
     "outcome and resolver are written by a validator through an Action, against "
     "the Ontology object - never by a build."),

    ("tests/test_nhs_number.py", "milliseconds",
     "The identifier check",
     [], None),

    ("tests/test_clocks.py", "the ones that matter",
     "One test per rule in the national guidance",
     [("", "Why these read like guidance and not like code",
       "<p>Each test is named after the rule it defends, so breaking a rule fails "
       "a sentence rather than moving a percentage. "
       "<code>test_weeks_are_per_clock_and_never_summed</code> is the one to look "
       "for: if it is missing, ask how they know.</p>")],
     None),

    ("tests/test_measures.py", "milliseconds",
     "Invariants, the frozen snapshot, and the off-by-one that moves the national figure",
     [], None),

    ("tests/test_providers.py", "milliseconds",
     "Succession, and the ugliest defect in the sample data",
     [], None),
]

KEYWORDS = {
    "def", "class", "return", "import", "from", "for", "in", "if", "else", "elif",
    "not", "and", "or", "is", "None", "True", "False", "with", "as", "assert",
    "lambda", "raise", "try", "except", "while", "break", "continue", "yield",
}


def highlight(code: str) -> str:
    out, in_doc = [], False
    for line in code.split("\n"):
        esc = html.escape(line)
        triples = esc.count('"""') + esc.count("'''")
        if in_doc or triples:
            out.append(f'<span class="c">{esc}</span>')
            if triples % 2:
                in_doc = not in_doc
            continue
        m = re.search(r"(\s#.*|^#.*)$", esc)
        if m:
            esc = esc[: m.start()] + f'<span class="c">{m.group(0)}</span>'
        out.append(esc)
    return "\n".join(out)


def split_module(path: Path):
    src = path.read_text().rstrip("\n")
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or ""
    if doc:
        lines = src.split("\n")
        end = tree.body[0].end_lineno
        body = "\n".join(lines[end:]).lstrip("\n")
    else:
        body = src
    return doc, body, len(src.split("\n"))


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<![\w/.])([a-z_]+\.py|[a-z_]+/|[a-z_]+\(\))", r"<code>\1</code>", text)
    return text


LIST_RE = re.compile(r"^\s*(\d+\.|[*-])\s+(.*)$")


def prose(doc: str) -> str:
    out = []
    for block in doc.strip().split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        items = [LIST_RE.match(l) for l in lines]

        # a lead sentence followed by a list
        if any(items):
            lead = [l for l, m in zip(lines, items) if not m]
            if lead:
                out.append(f"<p>{inline(' '.join(l.strip() for l in lead))}</p>")
            first = next(m for m in items if m)
            tag = "ol" if first.group(1).endswith(".") else "ul"
            bullets = "".join(
                f"<li>{inline(m.group(2).strip())}</li>" for m in items if m
            )
            out.append(f"<{tag}>{bullets}</{tag}>")
            continue

        # an indented block is a figure, not a sentence
        if all(l.startswith("  ") for l in lines):
            out.append(f"<pre><code>{highlight(block.rstrip())}</code></pre>")
            continue

        out.append(f"<p>{inline(' '.join(l.strip() for l in lines))}</p>")
    return "\n        ".join(out)


def section(path, rail_note, heading, notes, must_not) -> str:
    p = REPO / path
    doc, body, n = split_module(p)
    bits = [
        '  <section>',
        '    <div class="row">',
        f'      <div class="rail">{html.escape(Path(path).name)}<em>{n} lines &middot; {rail_note}</em></div>',
        '      <div>',
        f'        <h3 class="f">{heading}</h3>',
        f'        {prose(doc)}',
        f'        <pre><code>{highlight(body)}</code></pre>',
    ]
    for kind, tag, body_html in notes:
        cls = f"note {kind}".strip()
        inner = body_html if body_html.lstrip().startswith("<p") else f"<p>{body_html}</p>"
        bits.append(f'        <div class="{cls}"><span class="tag">{tag}</span>{inner}</div>')
    if must_not:
        bits.append(f'        <div class="never"><b>Must not</b>{must_not}</div>')
    bits += ['      </div>', '    </div>', '  </section>', '']
    return "\n".join(bits)


HEAD_END = '<div class="wrap">'

OPENING = """
  <div class="trainer-banner">
    <b>Trainer copy</b>
    <span>A model Project 1 repository, in full. Diff a trainee's repo against it; do not hand it out, or you have written the pipeline for them.</span>
  </div>

  <header class="masthead">
    <div class="eyebrow">Foundry Capstone &middot; Project 1 &middot; Repository</div>
    <h1>Rules and Wiring</h1>
    <p class="standfirst">Every file of a working Project 1 pipeline, with the code that should be in it &mdash; and the one structural rule that decides whether you can test the thing in milliseconds or only by building it.</p>
    <div class="budget">
      <div><b>Files</b><span>14</span></div>
      <div><b>Given to them</b><span>2</span></div>
      <div><b>Foundry imports in <code>rules/</code></b><span>0</span></div>
      <div><b>Tests needing a build</b><span>0</span></div>
      <div><b>Seed cohort</b><span>22 <small>pathways</small></span></div>
      <div><b>Compliance</b><span>38.5<small>%</small></span></div>
    </div>
  </header>

  <section>
    <div class="row">
      <div class="rail">&mdash;<em>the one rule</em></div>
      <div>
        <h3 class="f">Business logic never imports Foundry</h3>
        <p class="lede">Everything else follows. <code>rules/</code> is plain Python and Polars that knows about RTT and nothing about the platform. <code>transforms/</code> is wiring that knows about the platform and holds no rules of its own.</p>
        <p>The payoff is concrete: the RTT logic becomes testable in milliseconds with no Spark session and no build, so a trainee writes thirty small tests instead of three slow ones. It is also the difference between a rule you can read and a rule buried in the middle of a dataframe chain.</p>
        <pre><code>rtt-pipeline/
├── README.md              <span class="c"># the assumptions log - assessed</span>
├── rules/                 <span class="c"># plain Python + Polars. No transforms import. Ever.</span>
│   ├── nhs_number.py      <span class="c"># Modulus 11</span>
│   ├── clocks.py          <span class="c"># sequencing, attachment, DNA verdicts</span>
│   ├── measures.py        <span class="c"># weeks, bands, status, compliance</span>
│   └── providers.py       <span class="c"># succession, resolution as at a date</span>
├── transforms/            <span class="c"># wiring. Reads, calls rules, writes.</span>
│   ├── reference.py
│   ├── referral.py
│   ├── activity.py
│   ├── pathway.py
│   └── publish.py
└── tests/                 <span class="c"># under a second. No build, no Spark.</span>
    ├── test_nhs_number.py
    ├── test_clocks.py
    ├── test_measures.py
    └── test_providers.py</code></pre>
        <div class="note">
          <span class="tag">What Foundry gives them</span>
          <p>A Python transform repository arrives with its own scaffold &mdash; a <code>src/</code> layout, a conda recipe, and the registration that makes transforms discoverable. That is template, not design. The tree above sits inside it, and a trainee restructuring the scaffold on day 1 should be redirected.</p>
        </div>
        <div class="note eli5">
          <span class="tag">ELI5</span>
          <p>The rules are the recipe. The transforms are the kitchen. You can check a recipe at the kitchen table; you should not have to switch the ovens on.</p>
        </div>
      </div>
    </div>
  </section>

  <section>
    <div class="row">
      <div class="rail">&mdash;<em>the API</em></div>
      <div>
        <h3 class="f">A note on which decorator</h3>
        <p>The transforms below use <code>@transform.using(...)</code>. Palantir's API reference documents <code>@lightweight</code> as <strong>deprecated</strong> and points to <code>transform.using()</code> in its place, so both spellings will be in front of trainees &mdash; existing repositories are full of the older form:</p>
        <pre><code><span class="c"># the older spelling, still in most existing repos</span>
@lightweight
@transform(my_input=Input('/in'), my_output=Output('/out'))
def compute(my_input, my_output):
    my_output.write_pandas(my_input.pandas())

<span class="c"># the current one, and a builder: .with_resources(), .with_container()</span>
@transform.using(my_input=Input('/in'), my_output=Output('/out'))
def compute(my_input, my_output):
    my_output.write_table(my_input.polars(lazy=True).collect())</code></pre>
        <p>Both give the compute function <code>LightweightInput</code> and <code>LightweightOutput</code> objects, which read with <code>.polars(lazy=False)</code>, <code>.pandas()</code>, <code>.arrow()</code> and write with <code>.write_table()</code>, <code>.write_pandas()</code>, <code>.write_dataframe()</code>. <code>@transform_polars(Output(...), name=Input(...))</code> is the thin single-output wrapper &mdash; the compute function takes <code>ctx</code> first, receives eager frames and returns one.</p>
        <div class="note trap">
          <span class="tag">Unverified, and worth a browser</span>
          <p>The API-reference pages above render for a fetch tool and were read directly. The narrative pages &mdash; <code>lightweight-api-evolution</code>, <code>lightweight-examples</code> &mdash; are JavaScript-rendered and returned navigation only, so the migration guidance here rests on the reference pages alone. Worth five minutes with a browser before this goes in front of anyone.</p>
        </div>
      </div>
    </div>
  </section>
"""

CLOSING = """
  <div class="layerbar"><h2>marking</h2><span>Five minutes, before reading a line of logic</span></div>

  <section>
    <div class="row">
      <div class="rail">&mdash;<em>routine</em></div>
      <div>
        <h3 class="f">Reading a repository in five minutes</h3>
        <ol>
          <li><strong>Does <code>rules/</code> import anything from <code>transforms</code>?</strong> One grep. If it does, the tests will be slow and thin, and you already know most of what you need.</li>
          <li><strong>Is there a hard-coded status code set?</strong> Another grep, for <code>"10"</code>. The <code>ref_rtt_status</code> feed exists to make it unnecessary.</li>
          <li><strong>How long do the tests take?</strong> Under a second is right. Minutes means integration tests wearing a unit-test filename.</li>
          <li><strong>Row counts.</strong> <code>pathway</code> 22 against <code>clean_referral</code> 19. Equal means the grain is wrong and every number downstream is wrong with it.</li>
          <li><strong>Is <code>weeks_waiting</code> computed in the transform or at runtime?</strong> In the dataset alone, it goes stale the moment a validator corrects a clock start.</li>
          <li><strong>Read the README.</strong> Two minutes, and it predicts the rest of the mark better than any single file.</li>
        </ol>
        <div class="note">
          <span class="tag">The number to check against</span>
          <p>Seed cohort: 22 pathways, 19 referrals, 13 incomplete, <strong>38.5% compliance</strong>, 6 Validation Tasks, 3 build log entries. <strong>36.4%</strong> is the diagnostic wrong answer &mdash; it means REF0013 (invalid NHS number) and REF0014 (event before clock start) were deleted rather than flagged.</p>
        </div>
      </div>
    </div>
  </section>

  <section>
    <div class="row">
      <div class="rail">&mdash;<em>provenance</em></div>
      <div>
        <h3 class="f">What was run, and what was not</h3>
        <p>The algorithms in this repository were re-implemented in stdlib Python and reconciled against the expected outputs, on the seed cohort and on the 12,000-pathway dev tier. Every content dataset matches exactly: <code>clean_referral</code>, <code>pathway</code>, <code>pathway_event</code>, <code>ptl_snapshot</code>, and the full set of findings.</p>
        <p><strong>The Polars expressions themselves have not been executed.</strong> Polars could not be installed in the environment this was written in, so what is verified is the logic, not the syntax. Run the tests before teaching from it.</p>
      </div>
    </div>
  </section>

  <footer>
    <span>Foundry Capstone &middot; Project 1 repository &middot; Trainer copy</span>
    <span>Generated from <code>rtt-pipeline/</code></span>
  </footer>

</div>
"""

LAYERS = {
    "rules/nhs_number.py": ("rules/", "Plain Python and Polars &middot; no platform imports &middot; where the correctness mark lives"),
    "transforms/reference.py": ("transforms/", "Reads, calls rules, routes, writes &middot; no judgements of its own"),
    "tests/test_nhs_number.py": ("tests/", "No build, no Spark, no Foundry &middot; the whole suite under a second"),
}


def main():
    head = TEMPLATE.read_text().split(HEAD_END)[0]
    head = head.replace("<title>Twelve Files</title>", "<title>Rules and Wiring</title>")
    # This page is code-heavy: give the code column room while prose stays at
    # a readable 70ch. Appended after the sheet so it wins on specificity ties.
    head = head.replace(
        "</style>",
        "  .wrap { max-width:calc(96ch + var(--rail) + 3rem); }\n"
        "  pre { max-width:none; }\n"
        "  .table-scroll { max-width:none; }\n"
        "</style>",
    )

    parts = [head, HEAD_END, OPENING]
    for path, rail, heading, notes, must_not in FILES:
        if path in LAYERS:
            name, blurb = LAYERS[path]
            parts.append(f'\n  <div class="layerbar"><h2>{name}</h2><span>{blurb}</span></div>\n')
        parts.append(section(path, rail, heading, notes, must_not))

    parts.append('\n  <div class="layerbar"><h2>README.md</h2><span>Two minutes, and it predicts the rest of the mark</span></div>\n')
    readme = (REPO / "README.md").read_text()
    parts.append(
        '  <section>\n    <div class="row">\n'
        f'      <div class="rail">README.md<em>{len(readme.splitlines())} lines &middot; assessed</em></div>\n'
        '      <div>\n        <h3 class="f">The assumptions log</h3>\n'
        '        <p>Every RTT implementation in the country rests on a stack of local assumptions. '
        'The difference between a good one and a bad one is whether they are written down. The model '
        'README below is the shape to expect &mdash; the judgement calls and which way each went, the '
        'parameters and their values, the compute justification, and what the author would not trust yet.</p>\n'
        f'        <pre><code>{html.escape(readme.rstrip())}</code></pre>\n'
        '        <div class="note">\n          <span class="tag">Why read it first</span>\n'
        '          <p>It takes two minutes and predicts the rest of the mark better than any single file. '
        'Someone who can write down what they decided and why has understood the domain. Someone whose '
        'README is the repository template has produced code that happens to run.</p>\n        </div>\n'
        '      </div>\n    </div>\n  </section>\n'
    )
    parts.append(CLOSING)

    OUT.write_text("".join(parts))
    print(f"wrote {OUT} ({len(''.join(parts)):,} bytes)")


if __name__ == "__main__":
    main()
