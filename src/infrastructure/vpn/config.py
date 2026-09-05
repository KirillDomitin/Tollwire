#!/usr/bin/python
# -*- coding: utf-8 -*-

CONTAINER_NAME = "amnezia-panel"

DEFAULT_AWG_CONFIG="""[Interface]
PrivateKey = {}
Address = 10.8.1.0/24
ListenPort = {}
Jc = 5
Jmin = 10
Jmax = 50
S1 = 116
S2 = 57
S3 = 33
S4 = 12
H1 = 1
H2 = 2
H3 = 3
H4 = 4
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on"""
