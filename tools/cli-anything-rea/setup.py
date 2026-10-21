from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-rea",
    version="1.0.0",
    description="Harness for REA (morluto/rea) on this Mac: source reading, tool catalog, dependency doctor, real runs",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    package_data={"cli_anything.rea": ["skills/*.md"], "cli_anything.rea.probes": ["*.mjs"]},
    install_requires=["click>=8.0", "prompt-toolkit>=3.0"],
    entry_points={"console_scripts": ["cli-anything-rea=cli_anything.rea.rea_cli:main"]},
    python_requires=">=3.10",
)
