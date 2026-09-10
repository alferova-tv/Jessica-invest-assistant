import hashlib
import ssl
import unittest

from jessica.sources import tinvest

EXPECTED = "D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31"


class CertificateTest(unittest.TestCase):
    def test_root_is_the_ministry_root(self):
        der = ssl.PEM_cert_to_DER_cert(tinvest.CA_BUNDLE.read_text())
        self.assertEqual(":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), EXPECTED)


if __name__ == "__main__":
    unittest.main()
