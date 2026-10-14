from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-myai",
    version="1.0.0",
    description="Read-only ops CLI for myai.carher.net (ai_token_dashboard on K3s)",
    long_description=open("cli_anything/myai/README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.myai": ["skills/*.md"]},
    install_requires=[
        "click>=8.0.0",
        "prompt-toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "cli-anything-myai=cli_anything.myai.myai_cli:main",
        ],
    },
    python_requires=">=3.10",
)
