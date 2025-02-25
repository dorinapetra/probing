import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="probing",
    version="0.0.1",
    author="Judit Acs",
    author_email="judit@sch.bme.hu",
    description="Probing Transformer models",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/juditacs/probing",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
    install_requires=[
        'pyyaml',
        'numpy',
        'torch>=1.7',
        'pandas',
        'transformers',
        'scikit-learn',
    ],
)
