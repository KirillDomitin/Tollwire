#!/usr/bin/python
# -*- coding: utf-8 -*-


class SSHServerError(Exception): ...


class InstallError(SSHServerError): ...


class CommandExecutionError(SSHServerError): ...


class InvalidServerConfigurationError(SSHServerError): ...


class DockerError(SSHServerError): ...
