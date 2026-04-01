from setuptools import setup, find_packages

setup(
    name="server-watchdog",
    version="1.0.0",
    description="RHEL8 system maintenance daemon with SELinux AVC alerting",
    author="matebuteler",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.10",
    install_requires=[
        "google-genai>=1.0.0",
    ],
    scripts=[
        "scripts/server-watchdog-monthly",
        "scripts/server-watchdog-avc-monitor",
        "scripts/server-watchdog-send-now",
        "scripts/server-watchdog-sampler",
    ],
)
