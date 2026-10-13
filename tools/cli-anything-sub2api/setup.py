from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-sub2api",
    version="1.0.0",
    description="Stateful command-line interface for operating a Sub2API deployment",
    long_description=open("cli_anything/sub2api/README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.sub2api": ["skills/*.md"]},
    install_requires=[
        "click>=8.0.0",
        "prompt-toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "cli-anything-sub2api=cli_anything.sub2api.sub2api_cli:main",
        ],
    },
    python_requires=">=3.10",
)
