<?php

/**
 * @license GPL-3.0 (as OpenEMR)
 */

namespace OpenEMR\Modules\CarePlix;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Services\AppointmentService;
use RestConfig;

class AppointmentStatusController
{
    public const VERSION = '1.0.0';

    public function update($pid, $eid): array
    {
        RestConfig::authorization_check('patients', 'appt');
        header('X-CarePlix-Connector: ' . self::VERSION);

        $data = json_decode(file_get_contents('php://input'), true);
        $status = is_array($data) ? (string) ($data['pc_apptstatus'] ?? '') : '';
        $comment = is_array($data) ? trim((string) ($data['comment'] ?? '')) : '';
        $service = new AppointmentService();

        if ($status === '' || !$service->isValidAppointmentStatus($status)) {
            return $this->fail(400, 'pc_apptstatus must be an option of the apptstat list');
        }
        $rows = $service->getAppointment($eid);
        // The appointment must belong to the patient in the path, like the core GET/DELETE routes.
        if (empty($rows) || (string) $rows[0]['pid'] !== (string) $pid) {
            return $this->fail(404, 'appointment not found for this patient');
        }

        $service->updateAppointmentStatus($eid, $status, $_SESSION['authUser'] ?? '');
        if ($comment !== '') {
            QueryUtils::sqlStatementThrowException(
                "UPDATE openemr_postcalendar_events SET pc_hometext = TRIM(CONCAT(COALESCE(pc_hometext, ''), ' — ', ?)) WHERE pc_eid = ?",
                [mb_substr($comment, 0, 255), $eid]
            );
        }

        $appointment = $service->getAppointment($eid)[0];
        $result = [
            'pc_eid' => $appointment['pc_eid'],
            'pid' => $appointment['pid'],
            'pc_apptstatus' => $appointment['pc_apptstatus'],
            'previous_status' => $rows[0]['pc_apptstatus'],
            'connector' => self::VERSION,
        ];
        RestConfig::apiLog($result, json_encode(['pc_apptstatus' => $status, 'comment' => $comment]));
        return $result;
    }

    private function fail(int $code, string $message): array
    {
        http_response_code($code);
        return ['error' => $message, 'connector' => self::VERSION];
    }
}
