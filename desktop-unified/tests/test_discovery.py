import socket, unittest
from unittest.mock import Mock, patch
from discovery import candidates, lamps, parse_wifi, probe, Discovery

class DiscoveryTests(unittest.TestCase):
    def test_ssid_dedupe_and_bands(self):
        text="""SSID 1 : test network
 BSSID 1 : 00:11:22:33:44:55
   Signal : 58%
   Band : 5 GHz
   Channel : 149
 BSSID 2 : 00:11:22:33:44:56
   Band : 2.4 GHz
   Channel : 6
   Signal : 64%
SSID 2 : 测试网络
  信号 : 52%
  信道 : 13
SSID 3 : SixGHz
  Band : 6 GHz
  Channel : 5
SSID 4 :
  信道 : 3
"""
        ls=parse_wifi(text)
        self.assertEqual(len(ls),3)
        self.assertEqual(ls[0],dict(ssid='test network',signal=64,channels=[149,6],has_24=True))
        self.assertTrue(ls[1]['has_24'])
        self.assertFalse(ls[2]['has_24'])
    def test_no_untrusted_off_subnet_addresses(self):
        c=candidates([{'address':'192.168.5.30','prefix':16,'physical':True},
                      {'address':'8.8.8.8','prefix':8,'physical':True},
                      {'address':'10.0.0.4','prefix':8,'physical':False}])
        self.assertEqual(len(c),253);self.assertNotIn('192.168.5.30',c)
        self.assertTrue(all(x.startswith('192.168.5.') for x in c))
    def test_readonly_probe_requires_a_lamp_reply(self):
        def fixture(rows):
            s=Mock();s.__enter__=Mock(return_value=s);s.__exit__=Mock(return_value=False)
            s.recv=Mock(side_effect=rows);return s
        valid=fixture([b'CAPS TOY_SOUND_LAN_V1 count=3\r\nOK LIGHT ',b'mode=MANUAL color=OFF R=0 Y=0 G=0\r\n'])
        x=probe('192.168.1.20',connect=lambda *a,**k:valid)
        self.assertEqual(x['kind'],'ESP32-C3')
        valid.sendall.assert_called_once_with(b'LIGHT STATUS\r\n')
        notlamp=fixture([b'HTTP/1.1 200 OK\r\n',b''])
        self.assertIsNone(probe('192.168.1.21',connect=lambda *a,**k:notlamp))
    def test_current_connected_endpoint_not_probed_again(self):
        checked=[]
        def fake(address):checked.append(address)
        active={'ip':'192.168.5.32','port':8080,'kind':'ESP32-C3','active':True}
        x=lamps([{'address':'192.168.5.30','prefix':24,'physical':True}],active,fake)
        self.assertEqual(x,[active]); self.assertNotIn(active['ip'],checked)
    def test_demo_does_not_start_network_activity(self):
        d=Discovery(Mock(controller=Mock(demo=True)))
        with self.assertRaises(ValueError):d.start('lamps')
        self.assertFalse(d.snapshot()['busy'])
