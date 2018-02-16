from setuptools import setup, packages

setup(
    name='baby_ai_game',
    version='0.0.1',
    keywords='memory, environment, agent, rl, openaigym, openai-gym, gym',
    install_requires=[
        'gym>=0.9.6',
        'numpy>=1.10.0',
        'pyqt5',
        'matplotlib'
    ]
)
