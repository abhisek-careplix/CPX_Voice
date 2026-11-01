<?php

/**
 * CarePlix Connector for OpenEMR.
 *
 * OpenEMR 7.0.3's standard API can create, read and delete appointments but not update them, so
 * an integration can only "cancel" by deleting the event. This module adds the missing update:
 *
 *   PUT /apis/default/api/patient/:pid/appointment/:eid
 *   {"pc_apptstatus": "x", "comment": "Cancelled by CarePlix: moved to an earlier slot"}
 *
 * It changes the status through OpenEMR's own AppointmentService (which also updates the patient
 * tracker), so the visit stays on the calendar as "x Canceled" with its history. Any status in
 * the apptstat list is accepted, e.g. "SMS Confirmed" for reminder confirmations.
 *
 * The route sits under the existing appointment resource, so the existing
 * user/appointment.write scope and the patients/appt ACL govern it; no new scope is needed.
 *
 * @license GPL-3.0 (as OpenEMR)
 */

use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Modules\CarePlix\AppointmentStatusController;

/**
 * @var \Symfony\Component\EventDispatcher\EventDispatcherInterface $eventDispatcher
 * @var \OpenEMR\Core\ModulesClassLoader                            $classLoader
 * @see \OpenEMR\Core\ModulesApplication::loadCustomModule
 */
$classLoader->registerNamespaceIfNotExists('OpenEMR\\Modules\\CarePlix\\', [__DIR__ . DIRECTORY_SEPARATOR . 'src']);

$eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, function (RestApiCreateEvent $event) {
    $event->addToRouteMap('PUT /api/patient/:pid/appointment/:eid', function ($pid, $eid) {
        return (new AppointmentStatusController())->update($pid, $eid);
    });
    return $event;
});
