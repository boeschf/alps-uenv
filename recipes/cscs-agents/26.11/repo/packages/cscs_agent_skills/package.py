# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin.build_systems.generic import Package

from spack.package import *


class CscsAgentSkills(Package):
    """Local workflow skills, without vendored upstream CSCS skills."""

    homepage = "https://docs.cscs.ch/agents/"
    has_code = False

    license("MIT")

    version("2026.11.20")

    def content_hash(self, content=None):
        if content is None:
            with open(join_path(self.package_dir, "package.py"), "rb") as f:
                content = f.read()
        files = join_path(self.package_dir, "files")
        paths = []
        for root, _, filenames in os.walk(files):
            paths.extend(join_path(root, filename) for filename in filenames)
        for path in sorted(paths):
            with open(path, "rb") as f:
                content += repr((os.path.relpath(path, self.package_dir), f.read())).encode("utf-8")
        return super().content_hash(content=content)

    def install(self, spec, prefix):
        install_tree(
            join_path(self.package_dir, "files"),
            join_path(prefix.share, "cscs-agent-skills", "skills"),
        )
