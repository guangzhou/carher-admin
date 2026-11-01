from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-mihomo",
    version="1.2.0",
    description="Harness for mihomo (the engine inside ShellCrash) on 198: subscription -> safe config -> localhost-only instance -> switch/measure exits",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    package_data={"cli_anything.mihomo": ["skills/*.md"]},
    install_requires=["click>=8.0", "prompt-toolkit>=3.0", "pyyaml>=6.0"],
    entry_points={"console_scripts": ["cli-anything-mihomo=cli_anything.mihomo.mihomo_cli:main"]},
    python_requires=">=3.10",
)
