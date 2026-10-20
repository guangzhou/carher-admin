from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-litellm",
    version="1.1.0",
    description="Agent-friendly CLI harness for LiteLLM, plus read-only views of the 198 deployment (source, patches, DB)",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    package_data={"cli_anything.litellm": ["skills/*.md"], "cli_anything.litellm.probes": ["*.py"]},
    install_requires=[
        "click>=8.0",
        "fastapi<0.116",
        "litellm[proxy]>=1.79,<1.80",
        "PyYAML>=6.0",
        "prompt-toolkit>=3.0",
        "starlette<0.47",
    ],
    entry_points={"console_scripts": ["cli-anything-litellm=cli_anything.litellm.litellm_cli:main"]},
    python_requires=">=3.10",
)
