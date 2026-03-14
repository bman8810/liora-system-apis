"""GraphQL query string constants for the Zocdoc Provider API."""

GET_INBOX_ROWS = """query getInboxRows($practiceId: String!, $fromAppointmentTime: OffsetDateTime!, $tableAppointmentStatuses: [AppointmentStatus!]!, $statusCountStatuses: [AppointmentStatus!]!, $intakeReviewStates: [IntakeReviewState!], $productType: ProviderProduct, $pageSize: Int!, $pageNumber: Int!, $sortType: AppointmentSortType, $sortByField: AppointmentSortByField, $providerIdsFilter: [String!], $locationIdsFilter: [String!], $appointmentSources: [AppointmentSource!], $patientName: String!) {
  statusCounts: appointmentStatusAggregates(request: {practiceId: $practiceId, appointmentStatuses: $statusCountStatuses, fromAppointmentTime: $fromAppointmentTime, productType: $productType, appointmentSources: $appointmentSources}) {
    status
    count
    __typename
  }
  practice(practiceId: $practiceId) {
    timeZone
    __typename
  }
  appointments(request: {practiceId: $practiceId, appointmentStatuses: $tableAppointmentStatuses, fromAppointmentTime: $fromAppointmentTime, productType: $productType, pageSize: $pageSize, pageNumber: $pageNumber, sortType: $sortType, sortByField: $sortByField, providerIdsFilter: $providerIdsFilter, locationIdsFilter: $locationIdsFilter, appointmentSources: $appointmentSources, patientName: $patientName, intakeReviewStates: $intakeReviewStates}) {
    pagesCount
    totalAppointmentsCount
    appointments {
      appointmentId
      appointmentSource
      appointmentStatus
      appointmentTimeUtc
      bookingTimeUtc
      lastUpdatedUtc
      lastUpdatedByPatientUtc
      patientType
      isInNetwork
      patient {
        firstName
        lastName
        __typename
      }
      procedure {
        name
        __typename
      }
      provider {
        fullName
        __typename
      }
      insurance {
        carrier {
          id
          name
          __typename
        }
        plan {
          id
          name
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""

GET_PHI_APPOINTMENT_DETAILS = """query getPhiAppointmentDetails($practiceId: String, $appointmentId: String, $appointmentSource: AppointmentSource, $shouldFetchIntake: Boolean!) {
  appointmentDetails(request: {practiceId: $practiceId, appointmentIdentifier: {appointmentId: $appointmentId, appointmentSource: $appointmentSource}}) {
    appointmentIdentifier {
      appointmentId
      appointmentSource
      __typename
    }
    appointmentStatus
    practiceId
    requestId
    appointmentTimeUtc
    bookingTimeUtc
    lastUpdatedUtc
    patientType
    bookedBy
    isInNetwork
    duration
    procedure {
      id
      name
      __typename
    }
    practice {
      id
      name
      timeZone
      __typename
    }
    provider {
      id
      fullName
      npi
      __typename
    }
    location {
      id
      address1
      address2
      city
      state
      zipCode
      phoneNumber
      practiceFacingName
      __typename
    }
    insurance {
      card {
        memberId
        frontImageUrl
        backImageUrl
        __typename
      }
      carrier {
        id
        name
        __typename
      }
      plan {
        id
        name
        networkType
        programType
        __typename
      }
      __typename
    }
    patient {
      id
      firstName
      lastName
      email
      dob
      sex
      phoneNumber
      note
      cloudId
      requestedToCallTimestamp
      address {
        address1
        address2
        city
        state
        zipCode
        __typename
      }
      __typename
    }
    appointmentHistory {
      cancellationDetails {
        cancellationReason
        cancelledAtTimestamp
        __typename
      }
      reschedulingDetails {
        previousAppointmentTime
        rescheduledAtTimestamp
        reason
        __typename
      }
      __typename
    }
    intake @include(if: $shouldFetchIntake) {
      intakePatientId
      intakeCompletionStatus
      forms {
        formId
        formType
        isRequired
        __typename
      }
      cards {
        cardId
        cardType
        hasImage
        carrierName
        memberId
        __typename
      }
      __typename
    }
    __typename
  }
}"""

GET_STATUS_AGGREGATES = """query getAppointmentStatusAggregates($practiceId: String!, $fromAppointmentTime: OffsetDateTime!, $appointmentStatuses: [AppointmentStatus!]!, $productType: ProviderProduct, $appointmentSources: [AppointmentSource!]) {
  appointmentStatusAggregates(request: {practiceId: $practiceId, fromAppointmentTime: $fromAppointmentTime, appointmentStatuses: $appointmentStatuses, productType: $productType, appointmentSources: $appointmentSources}) {
    status
    count
    __typename
  }
}"""

MARK_INBOX_APPOINTMENT_STATUS = """mutation markInboxAppointmentStatus($practiceId: String!, $appointmentId: String!, $appointmentSource: AppointmentSource!, $status: AppointmentStatus!) {
  markInboxAppointmentStatus(request: {practiceId: $practiceId, appointmentIdentifier: {appointmentId: $appointmentId, appointmentSource: $appointmentSource}, status: $status}) {
    appointmentId
    appointmentSource
    appointmentStatus
    __typename
  }
}"""
