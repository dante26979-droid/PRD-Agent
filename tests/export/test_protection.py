from prd_agent.export.protection import AesGcmExternalIdProtector


def test_external_document_id_is_encrypted_and_round_trips():
    protector = AesGcmExternalIdProtector("local-development-encryption-key")

    protected = protector.protect("docx-private-id")

    assert "docx-private-id" not in protected
    assert protector.unprotect(protected) == "docx-private-id"
