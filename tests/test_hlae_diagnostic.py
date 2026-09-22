from scripts.misc.diagnose_hlae_capture import classify_plugin_log


def test_missing_client_callback_is_not_reported_as_failed_hlae_injection():
    log = "Connected to WebSocket server.\nCreateInterface called with Source2GameClients001\n"
    assert classify_plugin_log(log, True) == "client_connection_callback_not_observed"


def test_frame_hook_and_recording_are_distinct_stages():
    log = "ClientFullyConnect: playerSlot=0\n"
    assert classify_plugin_log(log, True) == "client_connected_frame_hook_not_observed"
    log += "Hooked FrameStageNotify\n"
    assert classify_plugin_log(log, True) == "frame_hook_ready_record_command_not_observed"
    # A scheduled command in the action list is not evidence of execution.
    log += '{"cmd":"mirv_streams record start","tick":6727}\n'
    assert classify_plugin_log(log, True) == "frame_hook_ready_record_command_not_observed"
    log += "[6727] Executed: mirv_streams record start\n"
    assert classify_plugin_log(log, True) == "record_command_executed"


def test_missing_plugin_log_does_not_override_module_evidence():
    assert classify_plugin_log("", True) == "hlae_loaded_plugin_initialization_not_observed"
    assert classify_plugin_log("", False) == "hlae_injection_not_observed"
