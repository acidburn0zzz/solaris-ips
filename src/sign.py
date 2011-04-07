#!/usr/bin/python2.6
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2010, 2011, Oracle and/or its affiliates. All rights reserved.
#

import getopt
import gettext
import locale
import os
import shutil
import sys
import tempfile
import traceback

import pkg
import pkg.actions as actions
import pkg.client.api_errors as api_errors
import pkg.client.transport.transport as transport
import pkg.fmri as fmri
import pkg.manifest as manifest
import pkg.misc as misc
import pkg.publish.transaction as trans
from pkg.client import global_settings
from pkg.misc import emsg, msg, PipeError

PKG_CLIENT_NAME = "pkgsign"

# pkg exit codes
EXIT_OK      = 0
EXIT_OOPS    = 1
EXIT_BADOPT  = 2
EXIT_PARTIAL = 3

repo_cache = {}

def error(text, cmd=None):
        """Emit an error message prefixed by the command name """

        if cmd:
                text = "%s: %s" % (cmd, text)
                
        else:
                text = "%s: %s" % (PKG_CLIENT_NAME, text)


        # If the message starts with whitespace, assume that it should come
        # *before* the command-name prefix.
        text_nows = text.lstrip()
        ws = text[:len(text) - len(text_nows)]

        # This has to be a constant value as we can't reliably get our actual
        # program name on all platforms.
        emsg(ws + text_nows)

def usage(usage_error=None, cmd=None, retcode=EXIT_BADOPT):
        """Emit a usage message and optionally prefix it with a more specific
        error message.  Causes program to exit."""

        if usage_error:
                error(usage_error, cmd=cmd)
        emsg (_("""\
Usage:
        pkgsign -s path_or_uri [-acik] [--no-index] [--no-catalog]
            [--sign-all | fmri-to-sign ...]
"""))

        sys.exit(retcode)

def fetch_catalog(src_pub, xport, temp_root, list_packages=False):
        """Fetch the catalog from src_uri."""

        if not src_pub.meta_root:
                # Create a temporary directory for catalog.
                cat_dir = tempfile.mkdtemp(dir=temp_root)
                src_pub.meta_root = cat_dir

        src_pub.transport = xport
        src_pub.refresh(True, True)

        if not list_packages:
                return
        
        cat = src_pub.catalog

        d = {}
        fmri_list = []
        for f in cat.fmris():
                fmri_list.append(f)
                d.setdefault(f.pkg_name, [f]).append(f)
        for k in d.keys():
                d[k].sort(reverse=True)

        return fmri_list

def main_func():
        misc.setlocale(locale.LC_ALL, "", error)
        gettext.install("pkg", "/usr/share/locale")
        global_settings.client_name = "pkgsign"

        try:
                opts, pargs = getopt.getopt(sys.argv[1:], "a:c:i:k:s:",
                    ["help", "no-index", "no-catalog", "sign-all"])
        except getopt.GetoptError, e:
                usage(_("illegal global option -- %s") % e.opt)

        show_usage = False
        sig_alg = "rsa-sha256"
        cert_path = None
        key_path = None
        chain_certs = []
        add_to_catalog = True
        set_alg = False
        sign_all = False

        repo_uri = os.getenv("PKG_REPO", None)
        for opt, arg in opts:
                if opt == "-a":
                        sig_alg = arg
                        set_alg = True
                elif opt == "-c":
                        cert_path = os.path.abspath(arg)
                        if not os.path.isfile(cert_path):
                                usage(_("%s was expected to be a certificate "
                                    "but isn't a file.") % cert_path)
                elif opt == "-i":
                        p = os.path.abspath(arg)
                        if not os.path.isfile(p):
                                usage(_("%s was expected to be a certificate "
                                    "but isn't a file.") % p)
                        chain_certs.append(p)
                elif opt == "-k":
                        key_path = os.path.abspath(arg)
                        if not os.path.isfile(key_path):
                                usage(_("%s was expected to be a key file "
                                    "but isn't a file.") % key_path)
                elif opt == "-s":
                        repo_uri = misc.parse_uri(arg)
                elif opt == "--help":
                        show_usage = True
                elif opt == "--no-catalog":
                        add_to_catalog = False
                elif opt == "--sign-all":
                        sign_all = True

        if show_usage:
                usage(retcode=EXIT_OK)

        if not repo_uri:
                usage(_("a repository must be provided"))

        if key_path and not cert_path:
                usage(_("If a key is given to sign with, its associated "
                    "certificate must be given."))

        if cert_path and not key_path:
                usage(_("If a certificate is given, its associated key must be "
                    "given."))

        if chain_certs and not cert_path:
                usage(_("Intermediate certificates are only valid if a key "
                    "and certificate are also provided."))

        if not pargs and not sign_all:
                usage(_("At least one fmri must be provided for signing."))

        if pargs and sign_all:
                usage(_("No fmris may be provided if the sign-all option is "
                    "set."))

        if not set_alg and not key_path:
                sig_alg = "sha256"

        s, h = actions.signature.SignatureAction.decompose_sig_alg(sig_alg)
        if h is None:
                usage(_("%s is not a recognized signature algorithm.") %
                    sig_alg)
        if s and not key_path:
                usage(_("Using %s as the signature algorithm requires that a "
                    "key and certificate pair be presented using the -k and -c "
                    "options.") % sig_alg)
        if not s and key_path:
                usage(_("The %s hash algorithm does not use a key or "
                    "certificate.  Do not use the -k or -c options with this "
                    "algorithm.") % sig_alg)

        errors = []

        t = misc.config_temp_root()
        temp_root = tempfile.mkdtemp(dir=t)
        del t
        
        cache_dir = tempfile.mkdtemp(dir=temp_root)
        incoming_dir = tempfile.mkdtemp(dir=temp_root)
        chash_dir = tempfile.mkdtemp(dir=temp_root)

        try:
                xport, xport_cfg = transport.setup_transport()
                xport_cfg.add_cache(cache_dir, readonly=False)
                xport_cfg.incoming_root = incoming_dir

                # Configure src publisher
                src_pub = transport.setup_publisher(repo_uri, "source", xport,
                    xport_cfg, remote_prefix=True)
                fmris = fetch_catalog(src_pub, xport, temp_root,
                    list_packages=sign_all)
                if not sign_all:
                        fmris = pargs
                successful_publish = False

                for pfmri in fmris:
                        try:
                                if isinstance(pfmri, basestring):
                                        pfmri = fmri.PkgFmri(pfmri)

                                # Get the existing manifest for the package to
                                # be sign.
                                m_str = xport.get_manifest(pfmri,
                                    content_only=True, pub=src_pub)
                                m = manifest.Manifest()
                                m.set_content(content=m_str)

                                # Construct the base signature action.
                                attrs = { "algorithm": sig_alg }
                                a = actions.signature.SignatureAction(cert_path,
                                    **attrs)
                                a.hash = cert_path

                                # Add the action to the manifest to be signed
                                # since the action signs itself.
                                m.add_action(a, misc.EmptyI)

                                # Set the signature value and certificate
                                # information for the signature action.
                                a.set_signature(m.gen_actions(),
                                    key_path=key_path, chain_paths=chain_certs,
                                    chash_dir=chash_dir)

                                # The hash of 'a' is currently a path, we need
                                # to find the hash of that file to allow
                                # comparison to existing signatures.
                                hsh = None
                                if cert_path:
                                        hsh, _dummy = \
                                            misc.get_data_digest(cert_path)

                                # Check whether the signature about to be added
                                # is identical, or almost identical, to existing
                                # signatures on the package.  Because 'a' has
                                # already been added to the manifest, it is
                                # generated by gen_actions_by_type, so the cnt
                                # must be 2 or higher to be an issue.
                                cnt = 0
                                almost_identical = False
                                for a2 in m.gen_actions_by_type("signature"):
                                        try:
                                                if a.identical(a2, hsh):
                                                        cnt += 1
                                        except api_errors.AlmostIdentical, e:
                                                e.pkg = pfmri
                                                errors.append(e)
                                                almost_identical = True
                                if almost_identical:
                                        continue
                                if cnt == 2:
                                        continue
                                elif cnt > 2:
                                        raise api_errors.DuplicateSignaturesAlreadyExist(pfmri)
                                assert cnt == 1, "Cnt was:%s" % cnt

                                # Append the finished signature action to the
                                # published manifest.
                                t = trans.Transaction(repo_uri,
                                    pkg_name=str(pfmri), xport=xport,
                                    pub=src_pub)
                                try:
                                        t.append()
                                        t.add(a)
                                        for c in chain_certs:
                                                t.add_file(c)
                                        t.close(add_to_catalog=add_to_catalog)
                                except:
                                        if t.trans_id:
                                                t.close(abandon=True)
                                        raise
                                msg(_("Signed %s") % pfmri)
                                successful_publish = True
                        except (api_errors.ApiException, fmri.FmriError,
                            trans.TransactionError), e:
                                errors.append(e)
                if errors:
                        error("\n".join([str(e) for e in errors]))
                        if successful_publish:
                                return EXIT_PARTIAL
                        else:
                                return EXIT_OOPS
                return EXIT_OK
        except api_errors.ApiException, e:
                error(e)
                return EXIT_OOPS
        finally:
                shutil.rmtree(temp_root)

#
# Establish a specific exit status which means: "python barfed an exception"
# so that we can more easily detect these in testing of the CLI commands.
#
if __name__ == "__main__":
        try:
                __ret = main_func()
        except (PipeError, KeyboardInterrupt):
                # We don't want to display any messages here to prevent
                # possible further broken pipe (EPIPE) errors.
                __ret = EXIT_OOPS
        except SystemExit, _e:
                raise _e
        except:
                traceback.print_exc()
                error(_("""\n
This is an internal error in pkg(5) version %(version)s.  Please let the
developers know about this problem by including the information above (and
this message) when filing a bug at:

%(bug_uri)s""") % { "version": pkg.VERSION, "bug_uri": misc.BUG_URI_CLI })
                __ret = 99
        sys.exit(__ret)
