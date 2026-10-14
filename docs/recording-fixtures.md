# Recording fixtures from a real agent

openvigie's adapters are built and tested against **recorded, scrubbed samples** of an agent's
files: tests never touch a real agent. This page records them for Hermes and OpenClaw (#55);
the same steps work for any agent the recorder knows.

The recorder (`scripts/record_fixture.py`) copies a small, allow-listed sample of the agent's
files and scrubs it, then writes two notes:

- `MANIFEST.md`: what was copied, plus questions for you;
- `SURVEY.md`: every file under the agent's directory (name, size and age, never content), and
  the agent's running processes and listening ports.

Scrubbing does this:

- Secrets are redacted.
- Credential files are never copied (`.env`, `auth*`, `*token*`, keys…).
- Home paths, user and host names, e-mails and IP addresses are replaced.
- Free text (prompts, replies, tool output, system prompts) becomes `[scrubbed text <= N chars]`.
- Structure, ids, timestamps, model names and usage numbers are kept.
- Finally, the output is checked again. If anything still looks like a secret, nothing is written.

## On the machine that runs the agents

Do this as **the user the agents run as**, so the recorder can read their files and see their
ports.

1. **Get the repository** (the recorder isn't part of the installed package):

   ```sh
   git clone https://github.com/victorbonnet/openvigie && cd openvigie
   uv sync
   git switch -c chore/fixtures-hermes-openclaw
   ```

2. **Note the versions and how each agent runs**, for the manifest:

   ```sh
   hermes --version; openclaw --version
   systemctl --user list-units --all | grep -Ei 'hermes|openclaw'   # or: launchctl list | grep -Ei …
   ```

3. **Make the agents do something.** Have one short conversation with each, with nothing
   private in it ("what's 2+2?" is enough), so the samples include a session with token usage.
   Leave the gateways running.

4. **Record**, replacing `<version>` with what step 2 printed. If you moved an agent's
   directory (for example with `HERMES_HOME` or `OPENCLAW_STATE_DIR`), use that path instead:

   ```sh
   uv run scripts/record_fixture.py hermes ~/.hermes \
       tests/adapters/hermes/fixtures/<version>/home/.hermes
   uv run scripts/record_fixture.py openclaw ~/.openclaw \
       tests/adapters/openclaw/fixtures/<version>/home/.openclaw
   ```

   If it stops with "secret patterns still match", it names the file and writes nothing. Tell
   me which file it was rather than forcing it through.

5. **Check what was found.** Open each `SURVEY.md`:
   - If an important file wasn't copied (session or usage data, often a `*.db`, `*.jsonl` or
     `*.json`), record again with `--include '<glob>'`. Remove the previous output first.
   - Files marked *(never copied)* stay out. If one of them seems to hold usage data, say so in
     the manifest and I'll find another way.

6. **Review before committing.**
   - Open each `MANIFEST.md` and answer the *To fill in by hand* questions. SURVEY.md has the
     processes and ports.
   - Read every file the manifest flags with **check it by hand** (logs are only
     pattern-redacted).
   - Skim the rest for anything personal: contact names, chat ids, project names in paths.
     Edit or delete what you find.

7. **Open a pull request** with only the fixtures:

   ```sh
   git add tests/adapters/hermes tests/adapters/openclaw
   git commit -m "chore(fixtures): scrubbed Hermes and OpenClaw samples (#55)"
   git push -u origin chore/fixtures-hermes-openclaw
   ```

CI runs a second secret scan on the PR. Once it's merged, the four adapter tasks (#56–#59) are
built from these files.

## Tips

- An idle sample helps too: record again into `…/<version>-idle/…` while the gateway runs with
  no conversation going.
- If an agent runs in Docker, record from the host directory it mounts. Mention the container
  in the manifest.
- Nothing is uploaded anywhere: the recorder only writes into the repository on your machine.
