from dataclasses import dataclass


@dataclass(frozen=True)
class ReglaRespuesta:
    palabras_clave: tuple[str, ...]
    respuesta: str


REGLAS: tuple[ReglaRespuesta, ...] = (
    ReglaRespuesta(
        palabras_clave=("hola", "buenos dias", "buenas tardes", "buenas noches"),
        respuesta=(
            "¡Hola! Soy ANMI, tu Asistente Nutricional Materno Infantil. "
            "¿En qué puedo ayudarte?"
        ),
    ),
    ReglaRespuesta(
        palabras_clave=("anemia", "hierro", "hemoglobina"),
        respuesta=(
            "La anemia ocurre cuando hay poca hemoglobina en la sangre. "
            "Los alimentos con hierro, como la sangrecita, el hígado y las carnes, "
            "pueden ayudar a prevenirla."
        ),
    ),
    ReglaRespuesta(
        palabras_clave=("lenteja", "lentejas", "menestra", "menestras"),
        respuesta=(
            "Las lentejas aportan hierro. Puedes combinarlas con alimentos ricos "
            "en vitamina C, como naranja, mandarina o tomate, para aprovechar mejor el hierro."
        ),
    ),
    ReglaRespuesta(
        palabras_clave=("sangrecita", "sangre de pollo"),
        respuesta=(
            "La sangrecita es una buena fuente de hierro. Debe cocinarse completamente "
            "antes de ofrecerla al bebé."
        ),
    ),
    ReglaRespuesta(
        palabras_clave=("6 meses", "seis meses", "alimentacion complementaria"),
        respuesta=(
            "Desde los 6 meses se inicia la alimentación complementaria, "
            "sin dejar la lactancia materna."
        ),
    ),
    ReglaRespuesta(
        palabras_clave=("gracias", "muchas gracias"),
        respuesta="¡Con gusto! Estoy aquí para ayudarte.",
    ),
    ReglaRespuesta(
        palabras_clave=("adios", "chau", "hasta luego"),
        respuesta="¡Hasta luego! Cuida mucho la alimentación de tu bebé.",
    ),
)


RESPUESTA_NO_ENCONTRADA = (
    "No entendí completamente tu consulta. Puedes preguntarme sobre anemia, "
    "hierro, sangrecita, lentejas o alimentación del bebé."
)