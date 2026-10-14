// Applies .github/repo-metadata.json: upserts labels and milestones, then puts issues that carry a
// milestone label into that milestone. Additive only: never deletes labels or milestones, and
// never overrides a milestone someone already set on an issue. Run via actions/github-script.
const fs = require("node:fs");

module.exports = async ({ github, context, core }, path = ".github/repo-metadata.json") => {
  const meta = JSON.parse(fs.readFileSync(path, "utf8"));
  const { owner, repo } = context.repo;

  const labels = await github.paginate(github.rest.issues.listLabelsForRepo, {
    owner,
    repo,
    per_page: 100,
  });
  const labelsByName = new Map(labels.map((l) => [l.name.toLowerCase(), l]));
  for (const want of meta.labels) {
    const color = want.color.replace(/^#/, "").toLowerCase();
    const description = want.description ?? "";
    const have = labelsByName.get(want.name.toLowerCase());
    if (!have) {
      await github.rest.issues.createLabel({ owner, repo, name: want.name, color, description });
      core.info(`label created: ${want.name}`);
    } else if (
      have.name !== want.name ||
      have.color.toLowerCase() !== color ||
      (have.description ?? "") !== description
    ) {
      await github.rest.issues.updateLabel({
        owner,
        repo,
        name: have.name,
        new_name: want.name,
        color,
        description,
      });
      core.info(`label updated: ${want.name}`);
    }
  }

  const milestones = await github.paginate(github.rest.issues.listMilestones, {
    owner,
    repo,
    state: "all",
    per_page: 100,
  });
  const milestonesByTitle = new Map(milestones.map((m) => [m.title, m]));
  const milestoneForLabel = new Map();
  for (const want of meta.milestones) {
    const description = want.description ?? "";
    let have = milestonesByTitle.get(want.title);
    if (!have) {
      ({ data: have } = await github.rest.issues.createMilestone({
        owner,
        repo,
        title: want.title,
        description,
      }));
      core.info(`milestone created: ${want.title}`);
    } else if ((have.description ?? "") !== description) {
      ({ data: have } = await github.rest.issues.updateMilestone({
        owner,
        repo,
        milestone_number: have.number,
        description,
      }));
      core.info(`milestone updated: ${want.title}`);
    }
    if (want.label) milestoneForLabel.set(want.label, have);
  }

  for (const [label, milestone] of milestoneForLabel) {
    const issues = await github.paginate(github.rest.issues.listForRepo, {
      owner,
      repo,
      labels: label,
      state: "all",
      per_page: 100,
    });
    for (const issue of issues) {
      if (issue.pull_request || issue.milestone) continue;
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: issue.number,
        milestone: milestone.number,
      });
      core.info(`#${issue.number} → ${milestone.title}`);
    }
  }
};
