"""Correct Paseo 0.16.1's mapper for the pinned OMP 18.22 approval choices.

Run against an npm-ci staging root, before install_tree (never an installed cache
hook). The upstream mapper only knows ['Approve', 'Deny']; OMP now puts grants
before 'Approve Once', and optionally offers session approval. Preserve the
existing bash/edit/write title/detail gate and validate the complete choice list.
Only the one-call answer is exposed; no grant/session choice is ever returned.

Sources: Paseo 0.16.1 rpc-ui-permission-mapper.js; OMP v18.22.0
packages/coding-agent/src/tools/approval.ts: approvalGrantChoices,
approvalChoiceLabels, approvalAnswerForChoice, sessionApprovalScope.
"""

import hashlib
from pathlib import Path
import sys


MAPPER = Path("node_modules/@getpaseo/server/dist/server/server/agent/providers/omp/rpc-ui-permission-mapper.js")
ORIGINAL_SHA256 = "a0d9f666f9d81477233ae6a5b86afc081f5468398070d4c9ab007ecb7cd690fa"
PATCHED_SHA256 = "54b9961591aafec1733a2da365ba38a6e4e1b56eadc8c70d11a6260eadbbad77"
REPLACEMENTS = (
    ('const TOOL_APPROVAL_OPTIONS = [TOOL_APPROVAL_APPROVE_VALUE, TOOL_APPROVAL_DENY_VALUE];\n', ''),
    ('    const provider = options.provider ?? OMP_PROVIDER;\n',
     '    const approveValue = toolApprovalOnceValue(event.options, descriptor.toolName);\n'
     '    if (!approveValue) {\n'
     '        return { kind: "passthrough" };\n'
     '    }\n'
     '    const provider = options.provider ?? OMP_PROVIDER;\n'),
    ('                    label: TOOL_APPROVAL_APPROVE_VALUE,\n',
     '                    label: approveValue,\n'),
    ('                approveValue: TOOL_APPROVAL_APPROVE_VALUE,\n',
     '                approveValue,\n'),
    ('    const approveValue = readString(request.metadata.approveValue) ?? TOOL_APPROVAL_APPROVE_VALUE;\n'
     '    const denyValue = readString(request.metadata.denyValue) ?? TOOL_APPROVAL_DENY_VALUE;\n'
     '    return { value: response.behavior === "allow" ? approveValue : denyValue };\n',
     '    const approveValue = request.metadata.approveValue;\n'
     '    const validOnce = approveValue === "Approve" || approveValue === "Approve Once";\n'
     '    return { value: response.behavior === "allow" && validOnce ? approveValue : TOOL_APPROVAL_DENY_VALUE };\n'),
    ('    if (event.method !== "select" || !hasExactToolApprovalOptions(event.options)) {\n',
     '    if (event.method !== "select") {\n'),
    ('function hasExactToolApprovalOptions(options) {\n'
     '    return (Array.isArray(options) &&\n'
     '        options.length === TOOL_APPROVAL_OPTIONS.length &&\n'
     '        options.every((option, index) => option === TOOL_APPROVAL_OPTIONS[index]));\n'
     '}\n',
     '''function toolApprovalOnceValue(options, toolName) {
    if (!Array.isArray(options) || options.some(option => typeof option !== "string")) {
        return null;
    }
    // Exact OMP 18.22 layout: grants, matching persistent grants, once,
    // optional tool-session choice, Deny. Unknown layouts remain ordinary forms.
    const grants = [];
    let index = 0;
    if (options[index]?.startsWith("Approve Directory: ")) {
        const folder = options[index].slice("Approve Directory: ".length);
        if (!folder || /[\\r\\n]/.test(folder)) return null;
        grants.push(options[index++]);
    }
    if (options[index] === "Approve File" ||
        (grants.length > 0 && options[index] === "Approve") ||
        (options[index] === "Approve" && options[index + 1] === "Always Approve")) {
        grants.push(options[index++]);
    }
    for (const grant of grants) {
        if (options[index++] !== `Always ${grant}`) return null;
    }
    const once = grants.length > 0 ? "Approve Once" : TOOL_APPROVAL_APPROVE_VALUE;
    if (options[index++] !== once) return null;
    if (options[index] === `Approve ${toolName} for this session`) index++;
    return options[index++] === TOOL_APPROVAL_DENY_VALUE && index === options.length ? once : null;
}
'''),
)


def corrected_source(source):
    for before, after in REPLACEMENTS:
        if source.count(before) != 1:
            raise RuntimeError("Pinned Paseo mapper source fragment changed")
        source = source.replace(before, after, 1)
    return source


def patch(staging_root):
    path = Path(staging_root) / MAPPER
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        return
    if digest != ORIGINAL_SHA256:
        raise RuntimeError(f"Refusing unknown Paseo OMP mapper: {path} (sha256 {digest})")
    corrected = corrected_source(original.decode("utf-8")).encode("utf-8")
    if hashlib.sha256(corrected).hexdigest() != PATCHED_SHA256:
        raise RuntimeError("Paseo mapper correction fingerprint mismatch")
    path.write_bytes(corrected)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_omp_approval.py NPM_STAGING_ROOT")
    patch(sys.argv[1])
