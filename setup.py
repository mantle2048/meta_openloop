from distutils.core import setup
from setuptools import find_packages

setup(
        name='open_loop',
        version='0.1.0',
        packages=find_packages(),
        license='MIT License',
        long_discription=open('README.md').read()
)
