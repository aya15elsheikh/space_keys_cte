import struct
import unittest
from datetime import datetime

from protocol import (
    AX25RepeatTracker,
    AX25StreamParser,
    CRCAlgorithm,
    SSPFrame,
    SSPStreamParser,
    build_ax25_s,
    build_ax25_ui,
    extract_ax25_frames_from_log,
    extract_ssp_frames_from_log,
    encode_stim_time,
    format_hex,
    hex_bytes,
    obc_time_seconds,
    crc16_reflected,
)
from catalog import SatelliteProfile, commands_for, node_by_id_for
from telemetry import decode_adcs, decode_comm, decode_frame, decode_obc, decode_payload, decode_power


class SSPTests(unittest.TestCase):
    def test_stim_uses_obc_2000_epoch_and_little_endian_u64_seconds(self):
        self.assertEqual(obc_time_seconds(datetime(2000, 1, 1)), 0)
        self.assertEqual(encode_stim_time(datetime(2000, 1, 1)), bytes(8))
        selected = datetime(2026, 9, 16, 12, 34, 56)
        expected_seconds = 842_877_296
        self.assertEqual(obc_time_seconds(selected), expected_seconds)
        self.assertEqual(encode_stim_time(selected), expected_seconds.to_bytes(8, "little"))
        raw = SSPFrame(0x01, 0x11, 0x11, encode_stim_time(selected)).encode(CRCAlgorithm.CCITT)
        decoded = SSPFrame.decode(raw, CRCAlgorithm.AUTO)
        self.assertTrue(decoded.valid)
        self.assertEqual(int.from_bytes(decoded.data, "little"), expected_seconds)

    def test_stim_rejects_dates_before_obc_epoch(self):
        with self.assertRaises(ValueError):
            encode_stim_time(datetime(1999, 12, 31, 23, 59, 59))

    def test_ccitt_matches_payload_ping_from_project(self):
        raw = SSPFrame(0x07, 0x01, 0x00).encode(CRCAlgorithm.CCITT)
        self.assertEqual(format_hex(raw), "C0 07 01 00 00 DC 0E C0")

    def test_ccitt_matches_adcs_ping_from_historical_log(self):
        raw = SSPFrame(0x05, 0x01, 0x00).encode(CRCAlgorithm.CCITT)
        self.assertEqual(format_hex(raw), "C0 05 01 00 00 AA 37 C0")

    def test_all_crc_variants_auto_detect(self):
        for algorithm in (CRCAlgorithm.CCITT, CRCAlgorithm.IBM, CRCAlgorithm.IBM_3740):
            raw = SSPFrame(7, 1, 0x25, b"\x04").encode(algorithm)
            decoded = SSPFrame.decode(raw, CRCAlgorithm.AUTO)
            self.assertTrue(decoded.valid)
            self.assertEqual(decoded.matched_crc, algorithm)

    def test_icd_crc_and_grad_command_table(self):
        self.assertEqual(crc16_reflected(b"123456789", CRCAlgorithm.IBM_3740), 0x29B1)
        commands = {item.name: item.identifier for item in commands_for(SatelliteProfile.GRAD_PROJECT)}
        self.assertEqual(commands, {
            "HI": 0x01, "ACK": 0x02, "NACK": 0x03, "PING": 0x04,
            "STIME": 0x05, "SMODE": 0x06, "GOTLM": 0x07,
            "GSTLM": 0x08, "SON": 0x09, "SOFF": 0x0A,
            "CIMG": 0x0C, "DIMG": 0x0D, "GIMG": 0x0E,
        })
        raw = SSPFrame(0xA1, 0xB0, commands["PING"]).encode(CRCAlgorithm.IBM_3740)
        self.assertEqual(SSPFrame.decode(raw, CRCAlgorithm.AUTO).matched_crc, CRCAlgorithm.IBM_3740)

    def test_stream_parser_split_noise_and_back_to_back(self):
        one = SSPFrame(7, 1, 0).encode(CRCAlgorithm.CCITT)
        two = SSPFrame(1, 7, 2, b"\x00").encode(CRCAlgorithm.CCITT)
        parser = SSPStreamParser(CRCAlgorithm.AUTO)
        self.assertEqual(parser.feed(b"noise" + one[:4]), [])
        frames = parser.feed(one[4:] + two)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.valid for frame in frames))

    def test_crc_failure_is_delivered_for_operator_analysis(self):
        raw = bytearray(SSPFrame(7, 1, 0).encode(CRCAlgorithm.CCITT))
        raw[-3] ^= 0x01
        frames = SSPStreamParser(CRCAlgorithm.AUTO).feed(raw)
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0].valid)

    def test_hex_parser_accepts_operator_formats(self):
        self.assertEqual(hex_bytes("C0 0x07,01:00-00 DC0E C0"), bytes.fromhex("C007010000DC0EC0"))

    def test_labview_log_replay_ignores_dates_and_preserves_split_frames(self):
        one = SSPFrame(0x01, 0x05, 0x46, struct.pack("<fff", 1.0, 2.0, 3.0)).encode(CRCAlgorithm.CCITT)
        split = format_hex(one[:11]) + "\n" + format_hex(one[11:])
        text = (
            "Transmited Frame: Thu, Sep 10, 2026 11:36:43 AM\n"
            "C0 07 01 00 00 DC 0E C0\n"
            "Received Frame: prose 5-8 seconds later\n" + split + "\n"
        )
        frames = extract_ssp_frames_from_log(text)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.valid for frame in frames))
        self.assertEqual(frames[1].data, struct.pack("<fff", 1.0, 2.0, 3.0))

    def test_payload_cimg_supports_obc_and_gcs_sources(self):
        command_data = bytes((4, 9, 1, 0, 0))
        for source in (0x01, 0x11):
            with self.subTest(source=source):
                command = SSPFrame(0x07, source, 0x2B, command_data)
                decoded_command = SSPFrame.decode(command.encode(CRCAlgorithm.CCITT), CRCAlgorithm.AUTO)
                self.assertTrue(decoded_command.valid)
                self.assertEqual((decoded_command.destination, decoded_command.source), (0x07, source))

                reply = SSPFrame(source, 0x07, 0x02, b"\x2B")
                decoded_reply = SSPFrame.decode(reply.encode(CRCAlgorithm.CCITT), CRCAlgorithm.AUTO)
                self.assertTrue(decoded_reply.valid)
                self.assertEqual((decoded_reply.destination, decoded_reply.source), (source, 0x07))

    def test_payload_timg_ack_routes_to_obc_and_gcs_sources(self):
        for source in (0x01, 0x11):
            with self.subTest(source=source):
                command = SSPFrame(0x07, source, 0x28)
                decoded_command = SSPFrame.decode(command.encode(CRCAlgorithm.CCITT), CRCAlgorithm.AUTO)
                self.assertTrue(decoded_command.valid)
                self.assertEqual((decoded_command.destination, decoded_command.source), (0x07, source))

                reply = SSPFrame(source, 0x07, 0x02, b"\x28")
                decoded_reply = SSPFrame.decode(reply.encode(CRCAlgorithm.CCITT), CRCAlgorithm.AUTO)
                self.assertTrue(decoded_reply.valid)
                self.assertEqual((decoded_reply.destination, decoded_reply.source), (source, 0x07))


class AX25Tests(unittest.TestCase):
    def test_original_satellite_session_vectors_match_supplied_capture(self):
        init_rr = build_ax25_s("RR", 1, destination="EGYGCS", source="ESKQUB")
        gostm_rnr = build_ax25_s("RNR", 2, destination="EGYGCS", source="ESKQUB")
        gostm_rr = build_ax25_s("RR", 2, destination="EGYGCS", source="ESKQUB")
        payload_data = bytes.fromhex(
            "87 00 00 00 00 00 00 00 74 00 00 00 00 00 00 00 "
            "00 CC 00 00 00 00 00 00 00 00 AA 00 00 00 00 AA "
            "00 00 EE 00 00 DA 00 13 A2 00 41 A9 10 4C DB 00 "
            "13 A2 00 41 AA C8 6D 00 00 00 00 00 00 00 00 00"
        )
        payload_ssp = SSPFrame(0x11, 0x07, 0xE5, payload_data).encode(CRCAlgorithm.CCITT)
        gostm_tlm = build_ax25_ui(
            payload_ssp, "EGYGCS", "ESKQUB", 2, 3, send_sequence=0, receive_sequence=2
        )

        self.assertEqual((init_rr[15], init_rr[-3:]), (0x21, bytes.fromhex("7E 37 7E")))
        self.assertEqual((gostm_rnr[15], gostm_rnr[-3:]), (0x45, bytes.fromhex("75 73 7E")))
        self.assertEqual((gostm_tlm[15], gostm_tlm[-3:]), (0x40, bytes.fromhex("D7 90 7E")))
        self.assertEqual(payload_ssp[-3:], bytes.fromhex("91 0E C0"))
        self.assertEqual((gostm_rr[15], gostm_rr[-3:]), (0x41, bytes.fromhex("D7 98 7E")))

    def test_round_trip_legacy_fixed_length_frame(self):
        ssp = SSPFrame(7, 1, 0).encode(CRCAlgorithm.CCITT)
        raw = build_ax25_ui(ssp, "ESKQUB", "EGYGCS", 2, 3, send_sequence=0)
        self.assertEqual(len(raw), 254)
        parser = AX25StreamParser()
        self.assertEqual(parser.feed(raw[:11]), [])
        frames = parser.feed(raw[11:])
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].valid_fcs)
        self.assertEqual(frames[0].payload, ssp)
        self.assertEqual(frames[0].destination, "ESKQUB")
        self.assertEqual(frames[0].source, "EGYGCS")
        self.assertEqual(frames[0].destination_ssid, 2)
        self.assertEqual(frames[0].source_ssid, 3)

    def test_fixed_length_parser_accepts_back_to_back_repeated_copies(self):
        ssp = SSPFrame(7, 1, 0).encode(CRCAlgorithm.CCITT)
        raw = build_ax25_ui(ssp, "ESKQUB", "EGYGCS", 2, 3, send_sequence=4)
        frames = AX25StreamParser().feed(raw + raw)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.valid_fcs for frame in frames))

    def test_flag_byte_inside_data_does_not_split_fixed_frame(self):
        # Command 0x0C produces a legitimate 0x7E byte in the embedded SSP
        # data area; only the fixed 254-byte boundary is a frame delimiter.
        ssp = SSPFrame(7, 1, 0x0C, b"X").encode(CRCAlgorithm.CCITT)
        raw = build_ax25_ui(ssp, "ESKQUB", "EGYGCS", 2, 3, send_sequence=2)
        self.assertIn(0x7E, raw[1:-1])
        frames = AX25StreamParser().feed(raw)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].valid_fcs)

    def test_original_gostm_downlink_is_three_logical_frames_with_three_copies_each(self):
        ssp = SSPFrame(0x11, 0x07, 0xE5, bytes(64)).encode(CRCAlgorithm.CCITT)
        rnr = build_ax25_s("RNR", 2, destination="EGYGCS", source="ESKQUB")
        telemetry = build_ax25_ui(
            ssp, "EGYGCS", "ESKQUB", 2, 3, send_sequence=0, receive_sequence=2
        )
        rr = build_ax25_s("RR", 2, destination="EGYGCS", source="ESKQUB")
        stream = rnr * 3 + telemetry * 3 + rr * 3

        frames = AX25StreamParser().feed(stream)
        tracker = AX25RepeatTracker()
        copies = [tracker.observe(frame.raw, now=index * 0.1) for index, frame in enumerate(frames)]

        self.assertEqual(len(frames), 9)
        self.assertTrue(all(frame.valid_fcs for frame in frames))
        self.assertEqual(copies, [1, 2, 3, 1, 2, 3, 1, 2, 3])
        self.assertEqual(
            [frames[index].control_description for index in (0, 3, 6)],
            ["S/RNR  N(R)=2", "I  N(S)=0  N(R)=2", "S/RR  N(R)=2"],
        )

    def test_labview_ax25_log_replay_preserves_all_three_physical_copies(self):
        ssp = SSPFrame(0x11, 0x07, 0xE5, bytes(64)).encode(CRCAlgorithm.CCITT)
        frame = build_ax25_ui(ssp, "EGYGCS", "ESKQUB", 2, 3, send_sequence=0, receive_sequence=2)
        text = "Received Frame: historical satellite session\n" + format_hex(frame * 3, 22)
        parsed = extract_ax25_frames_from_log(text)
        self.assertEqual(len(parsed), 3)
        self.assertTrue(all(item.raw == frame for item in parsed))


class TelemetryTests(unittest.TestCase):
    def test_grad_project_addresses_match_icd(self):
        nodes = node_by_id_for(SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(nodes[0xA1].name, "Core-OBC")
        self.assertEqual(nodes[0xA2].name, "Power / EPS")
        self.assertEqual(nodes[0xA3].name, "ADCS")
        self.assertEqual(nodes[0xA4].name, "Payload")
        self.assertEqual(nodes[0xA5].name, "S-Band")
        self.assertEqual(nodes[0xA6].name, "Core-COM")
        self.assertEqual(nodes[0xB0].name, "Ground Station")

    def test_grad_obc_layout_signed_position_and_duplicate_eps_records(self):
        data = bytearray(55)
        data[0], data[1] = 0xA1, 3
        data[2:10] = (123456789).to_bytes(8, "little")
        data[14:18] = (-31234567).to_bytes(4, "little", signed=True)
        data[18:22] = (29765432).to_bytes(4, "little", signed=True)
        data[43:45] = (17).to_bytes(2, "little")
        data[45:47] = (17).to_bytes(2, "little")
        decoded = decode_obc(bytes(data), SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(decoded["Longitude"], -31234567)
        self.assertEqual(decoded["Latitude"], 29765432)
        self.assertEqual(decoded["EPS SRecords (index 13)"], 17)
        self.assertEqual(decoded["EPS SRecords (index 14, ICD duplicate)"], 17)

    def test_grad_eps_adcs_comm_and_payload_layouts(self):
        eps = bytearray(42)
        eps[0] = 0xA2
        power_values = (5000, 120, 4980, 90, 5010, 210, 4970, 175, 6200, 340)
        for index, value in enumerate(power_values):
            eps[14 + index * 2:16 + index * 2] = value.to_bytes(2, "little")
        eps[34:36] = (0x0015).to_bytes(2, "little")
        eps[36:38] = (0x03FF).to_bytes(2, "little")
        eps[38:40] = (0x0008).to_bytes(2, "little")
        eps[40:42] = (17).to_bytes(2, "little")
        eps_decoded = decode_power(bytes(eps), SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(eps_decoded["OBC voltage (mV)"], 5000)
        self.assertEqual(eps_decoded["Battery current (mA)"], 340)
        self.assertEqual(eps_decoded["COMM current (mA) status"], "ADC saturated")
        self.assertEqual(eps_decoded["Payload control pin"], "HIGH")
        self.assertEqual(eps_decoded["Sample counter"], 17)
        self.assertEqual(eps_decoded["Current calibration"], "Configured")

        adcs = bytearray(52); adcs[0] = 0xA3; struct.pack_into("<f", adcs, 38, -12.5); adcs[50:52] = (123).to_bytes(2, "little")
        adcs_decoded = decode_adcs(bytes(adcs), SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(adcs_decoded["Magnetometer X"], -12.5)
        self.assertEqual(adcs_decoded["Reaction wheel speed (rpm)"], 123)

        comm = bytearray(23); comm[0] = 0xA6; comm[14:16] = (10).to_bytes(2, "little"); comm[16:18] = (9).to_bytes(2, "little")
        self.assertEqual(decode_comm(bytes(comm), SatelliteProfile.GRAD_PROJECT)["Correct received frames"], 9)

        payload = bytearray(57); payload[0] = 0xA4; payload[16:20] = (64000).to_bytes(4, "little")
        self.assertEqual(decode_payload(bytes(payload), SatelliteProfile.GRAD_PROJECT)["Image 1 size (bytes)"], 64000)

    def test_grad_eps_hides_uncalibrated_current(self):
        eps = bytearray(42)
        eps[0] = 0xA2
        eps[14:16] = (5000).to_bytes(2, "little")
        eps[16:18] = (999).to_bytes(2, "little")
        eps[36:38] = (0x0001).to_bytes(2, "little")
        decoded = decode_power(bytes(eps), SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(decoded["OBC voltage (mV)"], 5000)
        self.assertIsNone(decoded["OBC current (mA)"])
        self.assertEqual(decoded["OBC current (mA) status"], "Calibration required")
        self.assertEqual(decoded["Current calibration"], "Required")

    def test_grad_frame_routing_uses_icd_0x47_reply(self):
        data = bytes([0xA1, 0]) + bytes(53)
        subsystem, decoded = decode_frame(SSPFrame(0xB0, 0xA1, 0x47, data), SatelliteProfile.GRAD_PROJECT)
        self.assertEqual(subsystem, "Core-OBC")
        self.assertEqual(decoded["Telemetry layout"], "Grad ICD v3.1 Table 12")

    def test_current_adcs_layout(self):
        data = bytearray(50)
        struct.pack_into(">h", data, 19, 256)
        struct.pack_into("<fff", data, 23, 1.0, 2.0, 3.0)
        struct.pack_into("<fff", data, 35, 4.0, 5.0, 6.0)
        decoded = decode_adcs(bytes(data))
        self.assertEqual(decoded["Reaction wheel speed (rpm)"], 1.0)
        self.assertEqual(decoded["Gyroscope Y (deg/s)"], 2.0)
        self.assertEqual(decoded["Magnetometer Z (uT)"], 6.0)

    def test_current_payload_layout(self):
        data = bytearray(55)
        data[0] = 0x07
        data[16] = 4
        data[27:31] = (123456).to_bytes(4, "big")
        data[32:34] = (321).to_bytes(2, "big")
        decoded = decode_payload(bytes(data))
        self.assertEqual(decoded["Payload mode"], "Image Payload")
        self.assertEqual(decoded["Cached image size (bytes)"], 123456)
        self.assertEqual(decoded["Last image segment"], 321)

    def test_legacy_64_byte_payload_uses_original_satellite_icd_offsets(self):
        data = bytearray(64)
        data[0] = 0x87
        data[7:9] = (116).to_bytes(2, "big")
        data[0x11] = 0xCC
        data[0x1A] = 0xAA
        data[0x1B:0x1F] = (0x12345678).to_bytes(4, "little")
        data[0x1F] = 0xAA
        data[0x20:0x22] = (0x0102).to_bytes(2, "big")
        data[0x22] = 0xEE
        data[0x23:0x25] = (7).to_bytes(2, "big")
        data[0x25] = 0xDA
        data[0x26:0x2E] = bytes.fromhex("00 13 A2 00 41 A9 10 4C")
        data[0x2E] = 0xDB
        data[0x2F:0x37] = bytes.fromhex("00 13 A2 00 41 AA C8 6D")
        decoded = decode_payload(bytes(data))
        self.assertEqual(decoded["Payload telemetry layout"], "Original satellite ICD (64 bytes)")
        self.assertEqual(decoded["Payload mode"], "Imaging mode")
        self.assertEqual(decoded["Legacy time (seconds)"], 116)
        self.assertEqual(decoded["Image size (bytes)"], 0x12345678)
        self.assertEqual(decoded["SPI ACK"], 0x0102)
        self.assertEqual(decoded["Image error / SPI NACK"], 7)
        self.assertEqual(decoded["MAC address A"], "00:13:A2:00:41:A9:10:4C")
        self.assertEqual(decoded["MAC address B"], "00:13:A2:00:41:AA:C8:6D")

    def test_satellite_status_accepts_historical_zero_subsystem_header(self):
        data = bytearray(20)
        data[9:13] = (54).to_bytes(4, "little")
        data[15] = 2
        data[16:18] = (3405).to_bytes(2, "little")
        data[18:20] = (3158).to_bytes(2, "little")
        frame = SSPFrame(0x03, 0x01, 0xE7, bytes(data))
        subsystem, decoded = decode_frame(frame)
        self.assertEqual(subsystem, "Satellite")
        self.assertEqual(decoded["Status timestamp (low 32-bit OBT)"], 54)
        self.assertEqual(decoded["Power source"], "Battery")


if __name__ == "__main__":
    unittest.main()
