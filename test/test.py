import numpy as np
import matplotlib.pyplot as plt

# Parameter settings
a = 2
b = 3

# Vertical asymptote position
x0 = -b / a

# Define the x-axis range while avoiding the asymptote
x_left = np.linspace(x0 - 5, x0 - 0.1, 400)
x_right = np.linspace(x0 + 0.1, x0 + 5, 400)

# Define function
def f(x):
    return (a * x) / (a * x + b)

# Plot
plt.figure(figsize=(8, 5))

plt.plot(x_left, f(x_left))
plt.plot(x_right, f(x_right))

# Draw horizontal asymptote y = 1
plt.axhline(1, linestyle="--")

# Draw vertical asymptote x = -b/a
plt.axvline(x0, linestyle="--")

plt.xlabel("x")
plt.ylabel("f(x)")
plt.title("Function: ax / (ax + b)")

plt.grid(True)
plt.show()
