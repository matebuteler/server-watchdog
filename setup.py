from setuptools import setup, find_packages

setup(
    name="server-watchdog",
    version="1.0.0",
    description="RHEL8 system maintenance daemon with SELinux AVC alerting",
    author="matebuteler",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.8",
    install_requires=[
        "google-generativeai>=0.5.0",
    ],
    scripts=[
        "scripts/server-watchdog-monthly",
        "scripts/server-watchdog-avc-monitor",
    ],
)
