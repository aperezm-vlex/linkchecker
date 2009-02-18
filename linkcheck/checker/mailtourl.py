# -*- coding: iso-8859-1 -*-
# Copyright (C) 2000-2009 Bastian Kleineidam
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
"""
Handle for mailto: links.
"""

import cgi
import re
import urllib
import smtplib
from email._parseaddr import AddressList
import sys

from . import urlbase
from .. import log, LOG_CHECK, strformat, url as urlutil
from ..dns import resolver
from ..network import iputil
from .const import WARN_MAIL_NO_MX_HOST, \
    WARN_MAIL_UNVERIFIED_ADDRESS, WARN_MAIL_NO_CONNECTION


def getaddresses (addr):
    """Return list of email addresses from given field value."""
    return [mail for name, mail in AddressList(addr).addresslist if mail]


def is_quoted (addr):
    """Return True iff address string is quoted."""
    return addr.startswith(u'"') and addr.endswith(u'"')


def is_literal (domain):
    """Return True iff domain string is a literal."""
    return domain.startswith(u'[') and domain.endswith(u']')


_remove_quoted = re.compile(ur'\\.').sub
_quotes = re.compile(ur'["\\]')
def is_missing_quote (addr):
    return _quotes.match(_remove_quoted(u"", addr[1:-1]))


# list of CGI keys to search for email addresses
EMAIL_CGI = ("to", "cc", "bcc")

class MailtoUrl (urlbase.UrlBase):
    """
    Url link with mailto scheme.
    """

    def build_url (self):
        """Call super.build_url(), extract list of mail addresses from URL,
        and check their syntax.
        """
        super(MailtoUrl, self).build_url()
        self.addresses = set()
        self.parse_addresses()
        if self.addresses:
            for addr in sorted(self.addresses):
                self.check_email_syntax(addr)
                if not self.valid:
                    break
        else:
            self.set_result(_("No mail addresses found in `%(url)s'.") % \
                {"url": self.url}, valid=False, overwrite=False)

    def parse_addresses (self):
        """Parse all mail addresses out of the URL target. Also parses
        optional CGI headers like "?to=foo@example.org".
        Stores parsed paddresses in the self.addresses set.
        """
        # cut off leading mailto: and unquote
        url = urllib.unquote(self.base_url[7:])
        # search for cc, bcc, to and store in headers
        mode = 0 # 0=default, 1=quote, 2=esc
        quote = None
        i = 0
        for i, c in enumerate(url):
            if mode == 0:
                if c == '?':
                    break
                elif c in '<"':
                    quote = c
                    mode = 1
                elif c == '\\':
                    mode = 2
            elif mode==1:
                if c == '"' and quote == '"':
                    mode = 0
                elif c == '>' and quote == '<':
                    mode = 0
            elif mode == 2:
                mode = 0
        if i < (len(url) - 1):
            self.addresses.update(getaddresses(url[:i]))
            try:
                headers = cgi.parse_qs(url[(i+1):], strict_parsing=True)
                for key, vals in headers.items():
                    if key.lower() in EMAIL_CGI:
                        # Only the first header value is added
                        self.addresses.update(getaddresses(urllib.unquote(vals[0])))
            except ValueError, err:
                self.add_warning(_("Error parsing CGI values: %s") % str(err))
        else:
            self.addresses.update(getaddresses(url))
        log.debug(LOG_CHECK, "addresses: %s", self.addresses)

    def check_email_syntax (self, mail):
        """Check email syntax. The relevant RFCs:
        - How to check names (memo):
          http://tools.ietf.org/html/rfc3696
        - Email address syntax
          http://tools.ietf.org/html/rfc2822
        - SMTP protocol
          http://tools.ietf.org/html/rfc5321#section-4.1.3
        - IPv6
          http://tools.ietf.org/html/rfc4291#section-2.2
        - Host syntax
          http://tools.ietf.org/html/rfc1123#section-2
        """
        # length checks

        # restrict email length to 256 characters
        # http://www.rfc-editor.org/errata_search.php?eid=1003
        if len(mail) > 256:
            self.set_result(_("Mail address `%(addr)s' too long. Allowed 256 chars, was %(length)d chars.") % \
            {"addr": mail, "length": len(mail)}, valid=False, overwrite=False)
            return
        if "@" not in mail:
            self.set_result(_("Missing `@' in mail address `%(addr)s'.") % \
            {"addr": mail}, valid=False, overwrite=False)
            return
        # note: be sure to use rsplit since "@" can occur in local part
        local, domain = mail.rsplit("@", 1)
        if not local:
            self.set_result(_("Missing local part of mail address `%(addr)s'.") % \
            {"addr": mail}, valid=False, overwrite=False)
            return
        if not domain:
            self.set_result(_("Missing domain part of mail address `%(addr)s'.") % \
            {"addr": mail}, valid=False, overwrite=False)
            return
        if len(local) > 64:
            self.set_result(_("Local part of mail address `%(addr)s' too long. Allowed 64 chars, was %(length)d chars.") % \
            {"addr": mail, "length": len(local)}, valid=False, overwrite=False)
            return
        if len(domain) > 255:
            self.set_result(_("Domain part of mail address `%(addr)s' too long. Allowed 255 chars, was %(length)d chars.") % \
            {"addr": mail, "length": len(local)}, valid=False, overwrite=False)
            return

        # local part syntax check

        # Rules taken from http://tools.ietf.org/html/rfc3696#section-3
        if is_quoted(local):
            if is_missing_quote(local):
                self.set_result(_("Unquoted double quote or backslash in mail address `%(addr)s'.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
        else:
            if local.startswith(u"."):
                self.set_result(_("Local part of mail address `%(addr)s' may not start with a dot.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
            if local.endswith(u"."):
                self.set_result(_("Local part of mail address `%(addr)s' may not end with a dot.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
            if u".." in local:
                self.set_result(_("Local part of mail address `%(addr)s' may not contain two dots.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
            for char in u'@ \\",[]':
                if char in local.replace(u"\\%s"%char, u""):
                    self.set_result(_("Local part of mail address `%(addr)s' contains unquoted character `%(char)s.") % \
                    {"addr": mail, "char": char}, valid=False, overwrite=False)
                    return

        # domain part syntax check

        if is_literal(domain):
            # it's an IP address
            ip = domain[1:-1]
            if ip.startswith(u"IPv6:"):
                ip = ip[5:]
            if not iputil.is_valid_ip(ip):
                self.set_result(_("Domain part of mail address `%(addr)s' has invalid IP.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
        else:
            # it's a domain name
            if not urlutil.is_safe_domain(domain):
                self.set_result(_("Invalid domain part of mail address `%(addr)s'.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return
            if domain.endswith(".") or domain.split(".")[-1].isdigit():
                self.set_result(_("Invalid top level domain part of mail address `%(addr)s'.") % \
                {"addr": mail}, valid=False, overwrite=False)
                return

    def check_connection (self):
        """
        Verify a list of email addresses. If one address fails,
        the whole list will fail.

        For each mail address we check the following things:
          1. Look up the MX DNS records. If we found no MX record,
             print an error.
          2. Check if one of the mail hosts accept an SMTP connection.
             Check hosts with higher priority first.
             If no host accepts SMTP, we print a warning.
          3. Try to verify the address with the VRFY command. If we got
             an answer, print the verified address as an info.
             If not, print a warning.
        """
        assert self.addresses
        for mail in sorted(self.addresses):
            self.check_smtp_domain(mail)
            if not self.valid:
                break

    def check_smtp_domain (self, mail):
        """
        Check a single mail address.
        """
        from ..dns.exception import DNSException
        log.debug(LOG_CHECK, "checking mail address %r", mail)
        mail = strformat.ascii_safe(mail)
        username, domain = mail.rsplit('@', 1)
        log.debug(LOG_CHECK, "looking up MX mailhost %r", domain)
        try:
            answers = resolver.query(domain, 'MX')
        except DNSException:
            answers = []
        if len(answers) == 0:
            self.add_warning(_("No MX mail host for %(domain)s found.") %
                            {'domain': domain},
                             tag=WARN_MAIL_NO_MX_HOST)
            try:
                answers = resolver.query(domain, 'A')
            except DNSException:
                answers = []
            if len(answers) == 0:
                self.set_result(_("No host for %(domain)s found.") %
                                 {'domain': domain}, valid=False,
                                 overwrite=True)
                return
            # set preference to zero
            mxdata = [(0, rdata.to_text(omit_final_dot=True))
                      for rdata in answers]
        else:
            from linkcheck.dns.rdtypes.mxbase import MXBase
            mxdata = [(rdata.preference,
                       rdata.exchange.to_text(omit_final_dot=True))
                       for rdata in answers if isinstance(rdata, MXBase)]
            if not mxdata:
                if hasattr(sys, 'frozen'):
                    # under py2exe the DNS code is not working
                    self.add_warning(_("Invalid DNS answer due to py2exe" \
                        " environment ignored."), tag=WARN_MAIL_NO_MX_HOST)
                    self.set_result(_("Ok"), valid=True, overwrite=False)
                else:
                    self.set_result(_("Got invalid DNS answer %(answer)s for %(domain)s.") %
                        {'answer': answers, 'domain': domain}, valid=False,
                        overwrite=True)
                return
            # sort according to preference (lower preference means this
            # host should be preferred)
            mxdata.sort()
        # debug output
        log.debug(LOG_CHECK, "found %d MX mailhosts:", len(answers))
        for preference, host in mxdata:
            log.debug(LOG_CHECK,
                "MX host %r, preference %d", host, preference)
        # connect
        self.check_smtp_connect(mxdata, username, domain)

    def check_smtp_connect (self, mxdata, username, domain):
        """
        Connect to SMTP servers and check emails.

        @param mxdata: list of (preference, host) tuples to check for
        @type mxdata: list
        @param username: the username to verify
        @type username: string
        """
        smtpconnect = 0
        for preference, host in mxdata:
            try:
                log.debug(LOG_CHECK,
                    "SMTP check for %r (preference %d)", host, preference)
                self.url_connection = smtplib.SMTP()
                if log.is_debug(LOG_CHECK):
                    self.url_connection.set_debuglevel(1)
                self.url_connection.connect(host)
                log.debug(LOG_CHECK, "SMTP connected!")
                smtpconnect = 1
                self.url_connection.helo()
                mailaddress = "%s@%s" % (username, domain)
                status, info = self.url_connection.verify(mailaddress)
                log.debug(LOG_CHECK, "SMTP info %d %r", status, info)
                d = {
                    'info': "%d %s" % (status, str(info)),
                    'mail': mailaddress,
                }
                if status == 250:
                    self.add_info(_("Verified address %(mail)s: %(info)s.") % d)
                # check for 25x status code which means that the address
                # could not be verified, but is sent anyway
                elif 250 < status  < 260:
                    self.add_info(_("Unverified but presumably valid"
                                    " address %(mail)s: %(info)s.") % d)
                else:
                    self.add_warning(_("Unverified address: %(info)s.") % d,
                     tag=WARN_MAIL_UNVERIFIED_ADDRESS)
            except smtplib.SMTPException, msg:
                self.add_warning(
                      _("MX mail host %(host)s did not accept connections: "
                        "%(error)s.") % {'host': host, 'error': str(msg)},
                        tag=WARN_MAIL_NO_CONNECTION)
            if smtpconnect:
                break
        if not smtpconnect:
            self.set_result(_("Could not connect, but syntax is correct"),
                overwrite=True)
        else:
            self.set_result(_("Found MX mail host %(host)s") % {'host': host},
                overwrite=True)

    def close_connection (self):
        """
        Close a possibly opened SMTP connection.
        """
        if self.url_connection is None:
            # no connection is open
            return
        connection = self.url_connection
        self.url_connection = None
        try:
            connection.quit()
        except smtplib.SMTPException:
            # ignore close errors
            pass

    def set_cache_keys (self):
        """
        The cache key is a comma separated list of emails.
        """
        emails = u",".join(sorted(self.addresses))
        self.cache_url_key = u"%s:%s" % (self.scheme, emails)
        assert isinstance(self.cache_url_key, unicode), self.cache_url_key
        # cache_content_key remains None, recursion is not allowed

    def can_get_content (self):
        """
        mailto: URLs do not have any content

        @return: False
        @rtype: bool
        """
        return False
