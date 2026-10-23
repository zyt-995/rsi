from setuptools import setup, find_packages

groups = ['src', 'src/benchmark', 'src/agents', 'src/runner']
packages = []
package_dir = {}
for base in groups:
    for name in find_packages(base):
        if name not in packages:
            packages.append(name)
            package_dir[name] = base + '/' + name.replace('.', '/')
setup(packages=packages, package_dir=package_dir, include_package_data=True,
      package_data={'swe_chain_evo': ['*.yaml']})
