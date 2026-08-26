"""Read-only Oracle queries for pending document attachment reports."""


REGULAR_ORDER_ESTIMATION_DETAILS_QUERY = """
    SELECT
        RegularOrder.EstimationNo AS EST_NO,
        TRIM(RegularOrder.ROrderNo) AS REGULAR_ORDER_NO,
        CostEstimation.EstDate AS EST_DATE,
        TestDocument.ENQUIRYDOCUMENTTYPECODE AS DOCUMENT_TYPE_CODE,
        TestDocument.FILENAME AS ENQUIRY_FILENAME,
        TestDocument.DOCID AS ENQUIRY_DOCID,
        PODocument.FILENAME AS PO_FILENAME,
        PODocument.DOCID AS PO_DOCID
    FROM RegularOrder
    LEFT JOIN CostEstimation
        ON CostEstimation.EstNo = RegularOrder.EstimationNo
    LEFT JOIN ENQUIRYDOCUMENTDETAILS TestDocumentEstimation
        ON TestDocumentEstimation.ESTNO = RegularOrder.EstimationNo
    LEFT JOIN ENQUIRYDOCUMENT TestDocument
        ON TestDocument.DOCID = TestDocumentEstimation.DOCID
    LEFT JOIN RegularOrder_PODocument PODocument
        ON TRIM(PODocument.ROrderNo) = TRIM(RegularOrder.ROrderNo)
    WHERE RegularOrder.EstimationNo IS NOT NULL
    ORDER BY
        RegularOrder.EstimationNo DESC,
        TRIM(RegularOrder.ROrderNo),
        TestDocument.DOCID,
        PODocument.DOCID
"""


EST_SHEET_PENDING_QUERY = """
    SELECT DISTINCT t.*
    FROM (
        SELECT
            CostEstimation.EstNo,
            CostEstimation.EstDate,
            CostEstimation.Req_Qty,
            CostEstimation.BookingRate,
            CostEstimation.AuthDate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ) AS PartyName,
            YarnCount.CountName,
            CostEstimation.RefrenceType,
            CostEstimation.RefrenceNo,
            Representative.Name AS RepName,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(CostEstimation.AuthDate, 'YYYYMMDD') AS DelayDays,
            WM_CONCAT(
                DISTINCT DeletedEnquiryDocumentDetails.IACANCELREMARKS
            ) AS IACANCELREMARKS
        FROM CostEstimation
        INNER JOIN PartyMaster OrderParty
            ON OrderParty.PartyCode = CostEstimation.PartyCode
        LEFT JOIN PartyMaster RPJParty
            ON RPJParty.PartyCode = CostEstimation.RPJInvoicePartyCode
        INNER JOIN YarnCount
            ON YarnCount.CountCode = CostEstimation.CountCode
        LEFT JOIN EnquiryDocumentDetails
            ON EnquiryDocumentDetails.EstNo = CostEstimation.EstNo
            AND NVL(
                EnquiryDocumentDetails.ENQUIRYDOCUMENTTYPECODE,
                0
            ) = 1
        INNER JOIN Representative
            ON Representative.Code = CostEstimation.RepCode
        LEFT JOIN DeletedEnquiryDocumentDetails
            ON DeletedEnquiryDocumentDetails.EstNo = CostEstimation.EstNo
            AND NVL(
                DeletedEnquiryDocumentDetails.ENQUIRYDOCUMENTTYPECODE,
                0
            ) = 1
        WHERE NVL(CostEstimation.AuthDate, 0)
            >= TO_CHAR(SYSDATE - 365, 'YYYYMMDD')
          AND CostEstimation.AuthStatus = 1
          AND EnquiryDocumentDetails.EstNo IS NULL
          AND CostEstimation.RefrenceType IS NOT NULL
        GROUP BY
            CostEstimation.EstNo,
            CostEstimation.EstDate,
            CostEstimation.Req_Qty,
            CostEstimation.BookingRate,
            CostEstimation.AuthDate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ),
            YarnCount.CountName,
            CostEstimation.RefrenceType,
            CostEstimation.RefrenceNo,
            Representative.Name,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(CostEstimation.AuthDate, 'YYYYMMDD')
    ) t
    ORDER BY t.RepName, t.DelayDays DESC, 1
"""


MIXING_SHEET_PENDING_QUERY = """
    SELECT DISTINCT t.*
    FROM (
        SELECT
            CostEstimation.EstNo,
            CostEstimation.EstDate,
            CostEstimation.Req_Qty,
            CostEstimation.BookingRate,
            CostEstimation.AuthDate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ) AS PartyName,
            YarnCount.CountName,
            CostEstimation.RefrenceType,
            CostEstimation.RefrenceNo,
            Representative.Name AS RepName,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(CostEstimation.AuthDate, 'YYYYMMDD') AS DelayDays,
            WM_CONCAT(
                DISTINCT DeletedEnquiryDocumentDetails.IACANCELREMARKS
            ) AS IACANCELREMARKS
        FROM CostEstimation
        INNER JOIN PartyMaster OrderParty
            ON OrderParty.PartyCode = CostEstimation.PartyCode
        LEFT JOIN PartyMaster RPJParty
            ON RPJParty.PartyCode = CostEstimation.RPJInvoicePartyCode
        INNER JOIN YarnCount
            ON YarnCount.CountCode = CostEstimation.CountCode
        LEFT JOIN EnquiryDocumentDetails
            ON EnquiryDocumentDetails.EstNo = CostEstimation.EstNo
            AND NVL(
                EnquiryDocumentDetails.ENQUIRYDOCUMENTTYPECODE,
                0
            ) = 2
        INNER JOIN Representative
            ON Representative.Code = CostEstimation.RepCode
        LEFT JOIN DeletedEnquiryDocumentDetails
            ON DeletedEnquiryDocumentDetails.EstNo = CostEstimation.EstNo
            AND NVL(
                DeletedEnquiryDocumentDetails.ENQUIRYDOCUMENTTYPECODE,
                0
            ) = 2
        WHERE NVL(CostEstimation.AuthDate, 0)
            >= TO_CHAR(SYSDATE - 365, 'YYYYMMDD')
          AND CostEstimation.AuthStatus = 1
          AND EnquiryDocumentDetails.EstNo IS NULL
          AND CostEstimation.RefrenceType <> 'New Shade'
          AND CostEstimation.RefrenceType <> 'Regular Shade'
          AND CostEstimation.REQ_QTY > 100
        GROUP BY
            CostEstimation.EstNo,
            CostEstimation.EstDate,
            CostEstimation.Req_Qty,
            CostEstimation.BookingRate,
            CostEstimation.AuthDate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ),
            YarnCount.CountName,
            CostEstimation.RefrenceType,
            CostEstimation.RefrenceNo,
            Representative.Name,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(CostEstimation.AuthDate, 'YYYYMMDD')
    ) t
    ORDER BY t.RepName, t.DelayDays DESC, 1
"""


PO_DOCUMENT_PENDING_QUERY = """
    SELECT *
    FROM (
        SELECT
            RegularOrder.ROrderNo,
            RegularOrder.ENTRYDATE,
            RegularOrder.Weight,
            RegularOrder.OrderDate,
            RegularOrder.Rate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ) AS PartyName,
            RegularOrder.EstimationNo,
            RegularOrder.PARTYORDERNO,
            YarnCount.CountName,
            RegularOrder.RefOrderNo,
            Representative.Name AS RepName,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(
                    TO_CHAR(
                        TO_DATE(
                            RegularOrder.ENTRYDATE,
                            'DD.MM.YYYY HH24:MI:SS'
                        ),
                        'YYYYMMDD'
                    ),
                    'YYYYMMDD'
                ) AS DelayDays,
            WM_CONCAT(
                Cancel_RegularOrder_PODocument.IACANCELREMARKS
            ) AS IACANCELREMARKS,
            WM_CONCAT(
                Cancel_RegularOrder_PODocument.ISOCANCELREMARKS
            ) AS ISOCANCELREMARKS
        FROM RegularOrder
        INNER JOIN PartyMaster OrderParty
            ON OrderParty.PartyCode = RegularOrder.PartyCode
        LEFT JOIN PartyMaster RPJParty
            ON RPJParty.PartyCode = RegularOrder.RPJInvoicePartyCode
        INNER JOIN YarnCount
            ON YarnCount.CountCode = RegularOrder.CountCode
        LEFT JOIN RegularOrder_PODocument
            ON RegularOrder_PODocument.ROrderNo = RegularOrder.ROrderNo
        INNER JOIN Representative
            ON Representative.Code = RegularOrder.REPRESENTATIVECODE
        LEFT JOIN Cancel_RegularOrder_PODocument
            ON Cancel_RegularOrder_PODocument.ROrderNo
                = RegularOrder.ROrderNo
        WHERE TO_CHAR(
                TO_DATE(
                    RegularOrder.ENTRYDATE,
                    'DD.MM.YYYY HH24:MI:SS'
                ),
                'YYYYMMDD'
            ) >= TO_CHAR(SYSDATE - 365, 'YYYYMMDD')
          AND RegularOrder_PODocument.ROrderNo IS NULL
          AND RegularOrder.ROrderNo NOT LIKE 'DM%'
          AND RegularOrder.Shortage <> 1
          AND RegularOrder.Shortage <> 2
        GROUP BY
            RegularOrder.ROrderNo,
            RegularOrder.ENTRYDATE,
            RegularOrder.Weight,
            RegularOrder.OrderDate,
            RegularOrder.Rate,
            (
                OrderParty.PartyName
                || DECODE(
                    RPJParty.PartyName,
                    NULL,
                    '',
                    ' (' || RPJParty.PartyName || ')'
                )
            ),
            RegularOrder.EstimationNo,
            RegularOrder.PARTYORDERNO,
            YarnCount.CountName,
            RegularOrder.RefOrderNo,
            Representative.Name,
            TO_DATE(TO_CHAR(SYSDATE, 'YYYYMMDD'), 'YYYYMMDD')
                - TO_DATE(
                    TO_CHAR(
                        TO_DATE(
                            RegularOrder.ENTRYDATE,
                            'DD.MM.YYYY HH24:MI:SS'
                        ),
                        'YYYYMMDD'
                    ),
                    'YYYYMMDD'
                )
    )
    ORDER BY RepName, DelayDays DESC, 1
"""
