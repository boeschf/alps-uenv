// Run with Node against a corrected disposable npm-ci tree, not a live profile:
// node test_omp_approval.mjs /absolute/path/to/npm-staging-root
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

if (process.argv.length !== 3) throw new Error("usage: node test_omp_approval.mjs NPM_STAGING_ROOT");
const mapper = await import(pathToFileURL(path.resolve(process.argv[2],
    "node_modules/@getpaseo/server/dist/server/server/agent/providers/omp/rpc-ui-permission-mapper.js")));
const { classifyOmpRpcUiPermissionRequest: classify, buildOmpRpcUiPermissionResponse: respond } = mapper;
const event = (options, overrides = {}) => ({
    id: "approval-regression", method: "select", title: "Allow tool: bash\nCommand: printf approval-smoke",
    options, ...overrides,
});
const accepted = [
    ["Approve", "Deny"],
    ["Approve", "Approve bash for this session", "Deny"],
    ["Approve", "Always Approve", "Approve Once", "Deny"],
    ["Approve File", "Always Approve File", "Approve Once", "Deny"],
    ["Approve Directory: /tmp/project", "Always Approve Directory: /tmp/project", "Approve Once", "Deny"],
    ["Approve Directory: /tmp/project", "Approve File", "Always Approve Directory: /tmp/project", "Always Approve File", "Approve Once", "Deny"],
    ["Approve Directory: /tmp/project", "Approve", "Always Approve Directory: /tmp/project", "Always Approve", "Approve Once", "Deny"],
];
for (const options of accepted) {
    const mapped = classify(event(options));
    assert.equal(mapped.kind, "tool");
    const once = options.includes("Approve Once") ? "Approve Once" : "Approve";
    assert.equal(mapped.request.kind, "tool");
    assert.deepEqual(mapped.request.actions.map(action => action.label), ["Deny", once]);
    assert.deepEqual(respond(mapped.request, { behavior: "allow" }), { value: once });
    assert.deepEqual(respond(mapped.request, { behavior: "deny" }), { value: "Deny" });
    assert.deepEqual(respond(mapped.request, { behavior: "anything-else" }), { value: "Deny" });
    assert.deepEqual(respond({ ...mapped.request, metadata: { ...mapped.request.metadata, approveValue: "Always Approve" } }, { behavior: "allow" }), { value: "Deny" });
}
for (const options of [
    [], null, ["Deny", "Approve"], ["Approve Once", "Deny"],
    ["Approve File", "Approve Once", "Deny"],
    ["Approve", "Always Approve", "Approve", "Deny"],
    ["Approve", "Approve write for this session", "Deny"],
    ["Approve", "Deny", "Always Approve"],
    ["Approve Directory: /a", "Always Approve Directory: /b", "Approve Once", "Deny"],
    ["Approve Directory: ", "Always Approve Directory: ", "Approve Once", "Deny"],
    ["Approve", "Always Approve", "Approve Once", "Deny", "unexpected"],
    ["Approve", {}, "Deny"],
]) assert.deepEqual(classify(event(options)), { kind: "passthrough" });
for (const overrides of [
    { method: "form" }, { method: "input" }, { title: "Which command should run?" },
    { title: "Allow tool: unknown\nCommand: printf approval-smoke" },
    { title: "Allow tool: bash\nQuestion: What command?" },
]) assert.deepEqual(classify(event(accepted[1], overrides)), { kind: "passthrough" });
for (const [tool, title] of [
    ["edit", "Allow tool: edit\nFile: /tmp/example"],
    ["write", "Allow tool: write\nPath: /tmp/example\nContent:\nexample"],
]) {
    const options = ["Approve File", "Always Approve File", "Approve Once", "Deny"];
    const mapped = classify(event(options, { title }));
    assert.equal(mapped.kind, "tool");
    assert.equal(mapped.request.name, tool);
    assert.deepEqual(respond(mapped.request, { behavior: "allow" }), { value: "Approve Once" });
}
assert.equal(respond({ kind: "form", metadata: {} }, { behavior: "allow" }), null);
console.log("OMP pinned approval mapper regression passed");
